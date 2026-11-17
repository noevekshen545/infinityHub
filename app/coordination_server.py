#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Infinity Coordination Server — децентрализованная сеть для Infinity AI Search System.

Один Python-файл без зависимостей. Загрузите его на свой VPS/веб-сервер и запустите:

    python3 coordination_server.py --port 8443 --name "Мой сервер"

После запуска сервер выдаст ссылку на координационный файл:

    http://ВАШ-IP:8443/coordination.json

Эту ссылку добавляют в настройках приложения (Настройки -> Сеть -> Координация).
Несколько пользователей добавляют одну и ту же ссылку — и их инстансы находят друг друга.

Что умеет сервер:
  * GET  /coordination.json          — координационный файл (манифест + реестр нод)
  * GET  /api/nodes                  — JSON-список подключённых нод
  * POST /api/announce               — регистрация ноды (повторяется нодой каждые ~30 сек)
  * POST /api/relay/send             — событие для ноды или канала (store-and-forward)
  * GET  /api/relay/poll             — long-poll: нода забирает свои события
  * GET  /api/status                 — статистика
  * GET  /api/peers                  — связанные серверы координации (цепочки)

Связывание серверов между собой (общение по цепочке):
    python3 coordination_server.py --port 8443 --peer http://другой-сервер:8443

Серверы обмениваются реестрами нод и пересылают события нодам, которых не знают.

Приватность:
  * Ноды могут анонсироваться скрыто (hidden): они маршрутизируются,
    но не попадают в реестр, каталог и gossip.
  * --private — сервер не отдаёт реестр нод вообще и не участвует в gossip.
"""

import argparse
import hashlib
import html
import json
import os
import re
import ssl
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import swallow

VERSION = "1.0.0"
TYPE = "infinity-coordination"

NODE_TTL = 300            # секунд без анонса -> нода считается оффлайн
EVENT_TTL = 7 * 24 * 3600 # 7 дней храним события релея
MAX_TARGETED = 500        # максимум событий в очереди на (node, channel)
MAX_BROADCAST = 500       # максимум широковещательных событий на канал
MAX_HOPS = 6              # максимум пересылок между серверами (защита от циклов)
GOSSIP_INTERVAL = 30      # секунд между обменами реестрами с соседними серверами
MAX_BODY = 8 * 1024 * 1024
MAX_EVENTS_PER_POLL = 200
MAX_NODES = 20000
DELIVERED_CAP = 10000

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.@:/]{1,128}$")
_CHAN_RE = re.compile(r"^[A-Za-z0-9_\-.@:]{1,160}$")

_LOCK = threading.RLock()
_NODES = {}          # node_id -> info
_QUEUES = {}         # (to_node, channel) -> deque of events  (to_node: "*" для broadcast)
_BROADCAST = {}      # channel -> deque of events (общие для всех нод)
_DELIVERED = {}      # (node, channel) -> set(msg_id)
_SEEN = {}           # msg_id -> ts (дедупликация пересылок)
_SERVER_ID = uuid.uuid4().hex
_STARTED = time.time()
_EVENTS_TOTAL = 0
_PERSIST_PENDING = False
_SERVER = None

DATA_FILE = ""
NAME = ""
BASE_URL = ""
PEERS = []  # список URL соседних серверов
PRIVATE = False  # --private: сервер не отдаёт реестр нод и не участвует в gossip


def _now():
    return time.time()


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _prune_nodes():
    cutoff = _now() - NODE_TTL
    for nid in list(_NODES):
        if _NODES[nid].get("last_seen", 0) < cutoff:
            _NODES.pop(nid, None)


def _prune_events():
    cutoff = _now() - EVENT_TTL
    for key in list(_QUEUES):
        q = _QUEUES[key]
        while q and q[0].get("ts", 0) < cutoff:
            q.popleft()
        if not q:
            del _QUEUES[key]
    for ch in list(_BROADCAST):
        q = _BROADCAST[ch]
        while q and q[0].get("ts", 0) < cutoff:
            q.popleft()
        if not q:
            del _BROADCAST[ch]
    for mid in list(_SEEN):
        if _SEEN[mid] < cutoff:
            del _SEEN[mid]
    for key in list(_DELIVERED):
        s = _DELIVERED[key]
        if len(s) > DELIVERED_CAP:
            _DELIVERED[key] = set(list(s)[-DELIVERED_CAP:])


def _mark_dirty():
    global _PERSIST_PENDING
    _PERSIST_PENDING = True


def _persist_state():
    global _PERSIST_PENDING
    if not DATA_FILE:
        return
    try:
        with _LOCK:
            data = {
                "server_id": _SERVER_ID,
                "name": NAME,
                "base_url": BASE_URL,
                "peers": PEERS,
                "nodes": {nid: n for nid, n in _NODES.items()},
            }
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
            _PERSIST_PENDING = False
    except Exception as e:
        _log("persist error: %s" % e)


def _load_state():
    global _SERVER_ID, NAME, BASE_URL, PEERS
    try:
        if DATA_FILE and os.path.exists(DATA_FILE):
            with open(DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _SERVER_ID = data.get("server_id") or _SERVER_ID
            NAME = data.get("name") or NAME
            BASE_URL = data.get("base_url") or BASE_URL
            for u in data.get("peers") or []:
                if u not in PEERS:
                    PEERS.append(u)
            for nid, n in (data.get("nodes") or {}).items():
                if _NAME_RE.match(str(nid) or "") and isinstance(n, dict):
                    _NODES[nid] = n
    except Exception:
        swallow.note('coordination_server._load_state:L170')
        pass


def _public_node(nid, n):
    """Версия записи ноды для публичной выдачи (None для скрытых нод)."""
    if (n or {}).get("hidden"):
        return None
    out = dict(n)
    out.pop("home", None)
    return out


def _coord_file(include_nodes=True):
    _prune_nodes()
    nodes = []
    if include_nodes and not PRIVATE:
        nodes = [n for n in (_public_node(i, n) for i, n in _NODES.items()) if n]
        nodes.sort(key=lambda x: x.get("name", ""))
    return {
        "type": TYPE,
        "version": VERSION,
        "server_id": _SERVER_ID,
        "name": NAME,
        "base_url": BASE_URL,
        "public_key": "",
        "updated": _now(),
        "node_count": len(nodes),
        "nodes": nodes,
    }


def _registry_payload():
    """Реестр для gossip/выдачи: скрытые ноды не передаются, при PRIVATE пусто."""
    _prune_nodes()
    out = {}
    if PRIVATE:
        return out
    for nid, n in _NODES.items():
        pub = _public_node(nid, n)
        if pub:
            out[nid] = pub
    return out


def _enqueue(to_node, channel, event):
    key = (to_node, channel)
    q = _QUEUES.get(key)
    if q is None:
        q = deque()
        _QUEUES[key] = q
    q.append(event)
    while len(q) > MAX_TARGETED:
        q.popleft()


def _enqueue_broadcast(channel, event):
    q = _BROADCAST.get(channel)
    if q is None:
        q = deque()
        _BROADCAST[channel] = q
    q.append(event)
    while len(q) > MAX_BROADCAST:
        q.popleft()


def _make_event(to_node, channel, payload, from_node, msg_id=None, hop=0):
    global _EVENTS_TOTAL
    if not msg_id:
        h = hashlib.sha256()
        h.update(str(to_node).encode("utf-8", "replace"))
        h.update(str(channel).encode("utf-8", "replace"))
        h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace"))
        h.update(str(_now()).encode())
        h.update(str(from_node or "").encode("utf-8", "replace"))
        msg_id = h.hexdigest()[:24]
    evt = {
        "msg_id": msg_id,
        "ts": _now(),
        "to_node": to_node,
        "channel": channel,
        "from_node": from_node or "",
        "payload": payload,
        "hop": int(hop or 0),
    }
    _EVENTS_TOTAL += 1
    return evt


def _forward_to_url(url, evt):
    """Переслать событие конкретному соседнему серверу."""
    try:
        data = json.dumps({
            "to_node": evt["to_node"],
            "channel": evt["channel"],
            "from_node": evt["from_node"],
            "payload": evt["payload"],
            "msg_id": evt["msg_id"],
            "hop": evt.get("hop", 0) + 1,
        }).encode("utf-8")
        req = urllib.request.Request(url.rstrip("/") + "/api/relay/send", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(1 << 20)
        try:
            parsed = json.loads(body)
            return bool(parsed.get("ok"))
        except Exception:
            swallow.note('coordination_server._forward_to_url:L278')
            return False
    except Exception:
        swallow.note('coordination_server._forward_to_url:L281')
        return False


def _forward_to_peers(evt, skip=None):
    forwarded = []
    for peer in PEERS:
        if peer == skip:
            continue
        if _forward_to_url(peer, evt):
            forwarded.append(peer)
    return forwarded


def _route_event(evt):
    """Доставить событие локально или переслать на соседний сервер."""
    with _LOCK:
        _prune_nodes()
        if evt["msg_id"] in _SEEN:
            return {"ok": True, "duplicate": True}
        _SEEN[evt["msg_id"]] = _now()
        if len(_SEEN) > 100000:
            _prune_events()

        to_node = evt["to_node"]
        if to_node == "*":
            if _CHAN_RE.match(evt["channel"] or ""):
                _enqueue_broadcast(evt["channel"], evt)
                if evt.get("hop", 0) < MAX_HOPS:
                    _forward_to_peers(evt)
            return {"ok": True, "delivered": "broadcast"}

        node = _NODES.get(to_node)
        if node:
            if not _CHAN_RE.match(evt["channel"] or ""):
                return {"ok": False, "error": "bad channel"}
            home = node.get("home")
            my_base = BASE_URL or ""
            if home and my_base and home != my_base:
                if _forward_to_url(home, evt):
                    return {"ok": True, "delivered": "forwarded", "peers": [home]}
            _enqueue(to_node, evt["channel"], evt)
            return {"ok": True, "delivered": "local"}

        if evt.get("hop", 0) >= MAX_HOPS:
            return {"ok": False, "error": "no route (hops exceeded)"}

        forwarded = _forward_to_peers(evt)
        if forwarded:
            return {"ok": True, "delivered": "forwarded", "peers": forwarded}
        return {"ok": False, "error": "node unknown"}


def _merge_registry(remote_nodes):
    added = 0
    for nid, n in (remote_nodes or {}).items():
        if not _NAME_RE.match(str(nid) or ""):
            continue
        cur = _NODES.get(nid)
        if not cur or (n.get("last_seen") or 0) > (cur.get("last_seen") or 0):
            if not n.get("home"):
                n["home"] = (cur or {}).get("home") or ""
            _NODES[nid] = n
            added += 1
    if added and len(_NODES) <= MAX_NODES:
        _mark_dirty()
    return added


def _push_registry_to(peer):
    """Отдать свой реестр соседнему серверу (двусторонний gossip)."""
    with _LOCK:
        registry = _registry_payload()
    if not registry:
        return 0
    try:
        data = json.dumps({"nodes": registry}).encode("utf-8")
        req = urllib.request.Request(peer.rstrip("/") + "/api/gossip/push", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(8 << 20)
        try:
            parsed = json.loads(body)
            with _LOCK:
                return _merge_registry(parsed.get("nodes") or {})
        except Exception:
            swallow.note('coordination_server._push_registry_to:L367')
            return 0
    except Exception:
        swallow.note('coordination_server._push_registry_to:L370')
        return 0


def _gossip_once():
    if not PEERS:
        return
    if PRIVATE:
        return
    for peer in PEERS:
        try:
            url = peer.rstrip("/") + "/api/gossip/registry?since=0"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read(8 << 20))
            with _LOCK:
                added = _merge_registry(data.get("nodes") or {})
            added += _push_registry_to(peer)
            if added:
                _log("registry: +%d нод с %s" % (added, peer))
        except Exception:
            swallow.note('coordination_server._gossip_once:L390')
            pass


def _gossip_loop():
    while True:
        try:
            _gossip_once()
        except Exception:
            swallow.note('coordination_server._gossip_loop:L399')
            pass
        try:
            _prune_events()
            if _PERSIST_PENDING:
                _persist_state()
        except Exception:
            swallow.note('coordination_server._gossip_loop:L406')
            pass
        time.sleep(GOSSIP_INTERVAL)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "InfinityCoord/" + VERSION

    def log_message(self, fmt, *args):
        if not args:
            return
        msg = "%s - %s" % (self.address_string(), fmt % args)
        if "GET /api/relay/poll" in msg:
            return
        _log(msg)

    # -- helpers

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            swallow.note('coordination_server._read_body:L447')
            length = 0
        if length <= 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    def _read_json(self):
        body = self._read_body()
        if not body:
            return {}
        try:
            return json.loads(body)
        except Exception:
            swallow.note('coordination_server._read_json:L460')
            return {}

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _get(self, key, default=None):
        q = self._query()
        vals = q.get(key)
        if vals:
            return vals[0]
        return default

    def _origin(self):
        host = self.headers.get("Host") or "localhost"
        scheme = "https" if _SSL_CTX else "http"
        if BASE_URL:
            return BASE_URL
        return "%s://%s" % (scheme, host)

    # -- routes

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/coordination.json" or path == "/coordination":
                with _LOCK:
                    self._send_json(_coord_file())
            elif path == "/api/nodes":
                with _LOCK:
                    self._send_json({"ok": True, "updated": _now(), "nodes": _coord_file()["nodes"]})
            elif path == "/api/relay/poll":
                self._handle_poll()
            elif path == "/api/status":
                self._handle_status()
            elif path == "/api/peers":
                with _LOCK:
                    self._send_json({"ok": True, "peers": PEERS, "base_url": BASE_URL or self._origin()})
            elif path == "/api/gossip/registry":
                with _LOCK:
                    self._send_json({"ok": True, "server_id": _SERVER_ID,
                                     "base_url": BASE_URL or self._origin(),
                                     "nodes": _registry_payload()})
            elif path in ("/", "/index.html"):
                self._send_html()
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            swallow.note('coordination_server.do_GET:L508')
            pass
        except Exception as e:
            try:
                self._send_json({"error": str(e)}, 500)
            except Exception:
                swallow.note('coordination_server.do_GET:L514')
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/announce":
                self._handle_announce()
            elif path == "/api/relay/send":
                self._handle_send()
            elif path == "/api/relay/ack":
                self._handle_ack()
            elif path == "/api/gossip/push":
                body = self._read_json()
                with _LOCK:
                    added = _merge_registry(body.get("nodes") or {})
                    my = _registry_payload()
                self._send_json({"ok": True, "added": added, "nodes": my})
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            swallow.note('coordination_server.do_POST:L535')
            pass
        except Exception as e:
            try:
                self._send_json({"error": str(e)}, 500)
            except Exception:
                swallow.note('coordination_server.do_POST:L541')
                pass

    # -- handlers

    def _handle_announce(self):
        body = self._read_json()
        nid = str(body.get("node_id") or "").strip()
        if not nid or not _NAME_RE.match(nid):
            self._send_json({"ok": False, "error": "bad node_id"}, 400)
            return
        origin = self._origin()
        profiles = body.get("profiles")
        if not isinstance(profiles, list):
            profiles = []
        profiles = [p for p in profiles if isinstance(p, dict)][:50]
        info = {
            "node_id": nid,
            "name": str(body.get("name") or "")[:128],
            "url": str(body.get("url") or "")[:512],
            "onion_url": str(body.get("onion_url") or "")[:512],
            "public_key": str(body.get("public_key") or "")[:2048],
            "version": str(body.get("version") or "")[:64],
            "capabilities": list(body.get("capabilities") or [])[:32],
            "profile": body.get("profile") if isinstance(body.get("profile"), dict) else None,
            "profiles": profiles,
            "hidden": bool(body.get("hidden")),
            "first_seen": _NODES.get(nid, {}).get("first_seen") or _now(),
            "last_seen": _now(),
            "home": origin,
        }
        with _LOCK:
            if len(_NODES) >= MAX_NODES and nid not in _NODES:
                self._send_json({"ok": False, "error": "server full"}, 503)
                return
            _NODES[nid] = info
            _mark_dirty()
            count = len(_NODES)
        self._send_json({"ok": True, "server_id": _SERVER_ID, "base_url": origin,
                         "node_count": count})

    def _handle_send(self):
        body = self._read_json()
        to_node = str(body.get("to_node") or "")
        channel = str(body.get("channel") or "")
        payload = body.get("payload")
        if not to_node or not channel or not _CHAN_RE.match(channel):
            self._send_json({"ok": False, "error": "bad params"}, 400)
            return
        if payload is None:
            payload = {}
        try:
            raw = json.dumps(payload, ensure_ascii=False)
            if len(raw.encode("utf-8")) > MAX_BODY:
                self._send_json({"ok": False, "error": "payload too big"}, 413)
                return
        except Exception:
            self._send_json({"ok": False, "error": "bad payload"}, 400)
            return
        evt = _make_event(to_node, channel, payload,
                          from_node=str(body.get("from_node") or ""),
                          msg_id=str(body.get("msg_id") or ""),
                          hop=int(body.get("hop") or 0))
        result = _route_event(evt)
        self._send_json(result)

    def _handle_ack(self):
        body = self._read_json()
        node = str(body.get("node") or "")
        msg_ids = body.get("msg_ids") or []
        with _LOCK:
            for channel in list(_BROADCAST.keys()):
                if node:
                    key = (node, channel)
                    s = _DELIVED_GET(key)
                    if s is not None:
                        for mid in msg_ids:
                            s.discard(mid)
        self._send_json({"ok": True})

    def _handle_poll(self):
        node = self._get("node") or ""
        channels = [c for c in (self._get("channels") or "").split(",") if c]
        try:
            timeout = min(max(float(self._get("timeout") or 0), 0), 30)
        except ValueError:
            swallow.note('coordination_server._handle_poll:L627')
            timeout = 0
        if not node or not _NAME_RE.match(node):
            self._send_json({"ok": False, "error": "bad node"}, 400)
            return
        deadline = _now() + timeout
        out = []
        while True:
            with _LOCK:
                _prune_events()
                if channels:
                    for ch in channels:
                        if not _CHAN_RE.match(ch):
                            continue
                        key = (node, ch)
                        q = _QUEUES.get(key)
                        while q and len(out) < MAX_EVENTS_PER_POLL:
                            out.append(q.popleft())
                        if q is None or not q:
                            _QUEUES.pop(key, None)
                        bq = _BROADCAST.get(ch)
                        if bq:
                            mark = _DELIVED_GET((node, ch)) or set()
                            for evt in list(bq):
                                if evt["msg_id"] in mark:
                                    continue
                                mark.add(evt["msg_id"])
                                out.append(evt)
                                if len(out) >= MAX_EVENTS_PER_POLL:
                                    break
                            if mark:
                                _DELIVERED[(node, ch)] = mark
                if out:
                    self._send_json({"ok": True, "events": out})
                    return
            if _now() >= deadline:
                self._send_json({"ok": True, "events": []})
                return
            time.sleep(0.3)

    def _handle_status(self):
        with _LOCK:
            _prune_nodes()
            online = sum(1 for n in _NODES.values() if n.get("last_seen", 0) > _now() - NODE_TTL)
            self._send_json({
                "ok": True,
                "type": TYPE,
                "version": VERSION,
                "server_id": _SERVER_ID,
                "name": NAME,
                "base_url": BASE_URL or self._origin(),
                "uptime": int(_now() - _STARTED),
                "nodes_total": len(_NODES),
                "nodes_online": online,
                "peers": PEERS,
                "events_total": _EVENTS_TOTAL,
            })

    def _send_html(self):
        with _LOCK:
            _prune_nodes()
            nodes = sorted(_NODES.values(), key=lambda x: x.get("name", ""))
            if PRIVATE:
                nodes = []
            else:
                nodes = [n for n in nodes if not n.get("hidden")]
            now = _now()
            rows = []
            for n in nodes:
                name = html.escape(n.get("name") or n.get("node_id", ""))
                url = html.escape(n.get("url") or "")
                onion = html.escape(n.get("onion_url") or "")
                ver = html.escape(n.get("version") or "")
                online = "🟢 онлайн" if n.get("last_seen", 0) > now - NODE_TTL else "⚪ оффлайн"
                prof = ""
                if isinstance(n.get("profile"), dict) and n["profile"].get("display_name"):
                    prof = html.escape(n["profile"]["display_name"])
                rows.append(
                    "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (name, html.escape(n.get("node_id", "")), url or onion or "—", ver, online, prof)
                )
            coord_url = (BASE_URL or self._origin()) + "/coordination.json"
            page = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Infinity Coordination Server</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f1115;color:#e5e7eb;margin:0;padding:24px;max-width:980px;margin:0 auto}
h1{font-size:22px}code{background:#1c2027;padding:2px 8px;border-radius:6px;color:#7dd3fc}
table{width:100%%;border-collapse:collapse;margin-top:12px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #262b33;font-size:14px}
th{color:#9ca3af;font-weight:600;font-size:12px;text-transform:uppercase}
.box{background:#14171d;border:1px solid #262b33;border-radius:10px;padding:16px;margin-top:16px}
a{color:#7dd3fc}</style></head><body>
<h1>🌐 Infinity Coordination Server</h1>
<p>Сервер: <b>%s</b> (v%s)%s · нод: %d · онлайн: %d · соседних серверов: %d</p>
<div class="box"><b>Ссылка для приложения (координационный файл):</b><br>
<code>%s</code></div>
<div class="box"><b>Как добавить:</b><br>Приложение → Настройки → Сеть → Координация → «Добавить сервер» → вставить ссылку выше.</div>
<div class="box"><b>Связать с другим сервером (цепочка):</b><br>
<code>python3 coordination_server.py --port %d --peer %s/</code></div>
<h2>Ноды сети</h2>
<table><tr><th>Имя</th><th>ID</th><th>Адрес</th><th>Версия</th><th>Статус</th><th>Профиль</th></tr>%s</table>
</body></html>
""" % (html.escape(NAME), VERSION,
        " <span style='color:#f59e0b'>· ПРИВАТНЫЙ</span>" if PRIVATE else "",
        len(nodes),
        sum(1 for n in nodes if n.get("last_seen", 0) > now - NODE_TTL), len(PEERS),
        coord_url, self.server.server_address[1] if hasattr(self.server, "server_address") else 8443,
        html.escape(self._origin()), "".join(rows))
        self._send_text(page, ctype="text/html; charset=utf-8")


def _DELIVED_GET(key):
    return _DELIVERED.get(key)


# ---------------------------------------------------------------- main

_SSL_CTX = None


def _build_ssl_ctx(certfile, keyfile):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


def main(argv=None):
    global DATA_FILE, NAME, BASE_URL, PEERS, _SERVER, _SSL_CTX
    import sys
    try:
        sys.stdout.reconfigure(errors="replace", encoding="utf-8")
        sys.stderr.reconfigure(errors="replace", encoding="utf-8")
    except Exception:
        swallow.note('coordination_server.main:L760')
        pass
    ap = argparse.ArgumentParser(description="Infinity Coordination Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--name", default="Infinity Coordination")
    ap.add_argument("--data", default="", help="путь к файлу состояния (JSON)")
    ap.add_argument("--base-url", default="", help="внешний URL сервера, если за прокси")
    ap.add_argument("--peer", action="append", default=[], help="URL соседнего сервера (можно несколько)")
    ap.add_argument("--certfile", default="", help="TLS: путь к сертификату")
    ap.add_argument("--keyfile", default="", help="TLS: путь к ключу")
    ap.add_argument("--private", action="store_true",
                    help="приватный режим: не отдавать реестр нод и не обмениваться им с соседними серверами")
    args = ap.parse_args(argv)

    global PRIVATE
    NAME = args.name
    BASE_URL = (args.base_url or "").rstrip("/")
    PEERS = [p.rstrip("/") for p in args.peer if p]
    PRIVATE = bool(args.private)
    if args.data:
        DATA_FILE = args.data
    else:
        DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "coordination_data_%d.json" % args.port)
    _load_state()
    for p in args.peer:
        if p.rstrip("/") not in PEERS:
            PEERS.append(p.rstrip("/"))

    if args.certfile and args.keyfile:
        _SSL_CTX = _build_ssl_ctx(args.certfile, args.keyfile)

    _SERVER = ThreadingHTTPServer((args.host, args.port), Handler)
    if _SSL_CTX:
        _SERVER.socket = _SSL_CTX.wrap_socket(_SERVER.socket, server_side=True)

    threading.Thread(target=_gossip_loop, daemon=True).start()

    # показ IP для ссылки
    ip = "127.0.0.1"
    try:
        s = socket_sock()
        if s:
            ip = s
    except Exception:
        swallow.note('coordination_server.main:L806')
        pass
    scheme = "https" if _SSL_CTX else "http"
    if BASE_URL:
        link = BASE_URL + "/coordination.json"
    else:
        link = "%s://%s:%d/coordination.json" % (scheme, ip, args.port)

    _log("=" * 60)
    _log("Infinity Coordination Server v%s запущен" % VERSION)
    _log("Сервер:      %s://%s:%d" % (scheme, args.host, args.port))
    _log("Ссылка:      %s" % link)
    _log("Реестр нод:  %s/api/nodes" % link.replace("/coordination.json", ""))
    _log("Панель:      %s" % link.replace("/coordination.json", ""))
    if PRIVATE:
        _log("РЕЖИМ:       ПРИВАТНЫЙ — реестр нод скрыт, gossip отключён")
    _log("")
    _log("Добавьте ссылку в приложение: Настройки → Сеть → Координация")
    if PEERS:
        _log("Связанные серверы (цепочки): %s" % ", ".join(PEERS))
    _log("=" * 60)

    try:
        _SERVER.serve_forever()
    except KeyboardInterrupt:
        swallow.note('coordination_server.main:L831')
        _log("остановка...")
    finally:
        _persist_state()


def socket_sock():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        swallow.note('coordination_server.socket_sock:L846')
        return None


if __name__ == "__main__":
    main()
