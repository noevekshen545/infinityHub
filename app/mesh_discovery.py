#!/usr/bin/env python3
"""mesh_discovery — анонсы соседних нод по UDP и список кандидатов.

Флаг `decentral.mesh.discovery` до 2026-10-31 читался только санитайзером.
Здесь у него появляется единственный потребитель, и поведение ровно то, о
чём договаривались: анонс в локальную сеть и список найденных узлов. Никакого
автоподключения — адрес попадает в `decentral.grid.peers` только когда
администратор нажал «Добавить в сетку», и этот путь проходит через обычный
`client_offload.save`, то есть через те же проверки адреса, что и ручной ввод.

Почему только анонсы, а не «сам подключился»: сетевой сосед — это чужая
копия библиотеки с чужими файлами. Раздача поиска соседу означает, что строка
поиска пользователя уйдёт с этой машины. Такое решение не может принимать
порт, услышавший случайный широковещательный пакет.

Уровни флага:
  * выключен — сокет не создаётся, поток не поднимается, файл кандидатов не
    появляется; пакетов в сеть не уходит ни одного;
  * включён — анонс каждые interval_s и приёмник; кандидаты видны в панели.

Что в пакете: только адрес узла, имя машины и ключ подписи. Пользовательских
данных, имён файлов, запросов и статистики в анонсе нет по построению — пакет
собирает одна функция `_announce_packet`, и гейт проверяет её поля.
"""

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import swallow

logger = logging.getLogger("infinity")

MAGIC = "infmesh1"
DEFAULT_PORT = 8445
DEFAULT_GROUP = "infinity"
DEFAULT_INTERVAL_S = 15
DEFAULT_TTL_S = 90
MAX_CANDIDATES = 64
TS_SKEW_S = 300
MIN_INTERVAL_S = 5


def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('mesh_discovery._data_dir:L57')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


DISCOVERY_PATH = os.path.join(_data_dir(), "mesh_discovery.json")

_lock = threading.RLock()
_cand = {"loaded": False, "rec": {}}
_run = {"thread": None, "sock": None, "running": False, "port": 0,
        "group": "", "error": None, "stop": False, "sent": 0, "recv": 0,
        "rejects": 0, "last_reject": "",
        "started_at": 0.0, "last_send_at": 0.0, "last_recv_at": 0.0}
_selftest = {"url": None}   # только для гейтов: подставить свой адрес


# ── Конфиг ────────────────────────────────────────────────────────────────

def _mesh():
    try:
        import client_offload
        return client_offload.decentral().get("mesh") or {}
    except Exception:
        swallow.note('mesh_discovery._mesh:L79')
        return {}


def enabled():
    """Рубильник. Любая ошибка конфига означает «выключено», а не «попробуем»."""
    return bool(_mesh().get("discovery"))


def cfg():
    m = _mesh()

    def num(key, lo, hi, dflt):
        try:
            v = int(m.get(key))
        except Exception:
            swallow.note('mesh_discovery.num:L95')
            return dflt
        return max(lo, min(hi, v)) if isinstance(v, int) else dflt

    grp = _clean_group(m.get("discovery_group"))
    return {
        "enabled": enabled(),
        "port": num("discovery_port", 1024, 65533, DEFAULT_PORT),
        "group": grp or DEFAULT_GROUP,
        "interval_s": num("discovery_interval_s", MIN_INTERVAL_S, 3600,
                          DEFAULT_INTERVAL_S),
        "ttl_s": num("discovery_ttl_s", 15, 3600, DEFAULT_TTL_S),
    }


_GROUP_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def _clean_group(raw):
    s = str(raw or "").strip()
    return s if _GROUP_RE.match(s) else ""


# ── Свой адрес ────────────────────────────────────────────────────────────

def _own_ip():
    """Локальный адрес этой машины.

    UDP-сокет к публичному адресу ничего не отправляет: connect() на датаграмм
    -сокете только выбирает маршрут, поэтому способ работает и без интернета,
    а падение возвращает к перебору интерфейсов.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = str(s.getsockname()[0] or "")
        finally:
            s.close()
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        swallow.note('mesh_discovery._own_ip:L137')
        pass
    try:
        import client_offload
        for a in sorted(client_offload._own_address_set()):
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                swallow.note('mesh_discovery._own_ip:L145')
                continue
            if ip.version == 4 and not ip.is_loopback:
                return a
    except Exception:
        swallow.note('mesh_discovery._own_ip:L150')
        pass
    return "127.0.0.1"


def _own_port():
    try:
        import server
        p = int(getattr(server, "HTTP_PORT", 0) or 0)
        if p:
            return p
    except Exception:
        swallow.note('mesh_discovery._own_port:L162')
        pass
    try:
        return int(os.environ.get("INFINITY_PORT") or 8281)
    except Exception:
        swallow.note('mesh_discovery._own_port:L167')
        return 8281


def own_url():
    if _selftest["url"]:
        return str(_selftest["url"])
    ip = _own_ip()
    host = "[%s]" % ip if ":" in ip else ip
    return "http://%s:%d" % (host, _own_port())


def _broadcast_targets(port):
    """Куда слать анонс: широковещательный адрес плюс бродкаст каждого
    непетлевого интерфейса, если их видно."""
    out = {"255.255.255.255"}
    try:
        import psutil
        for entries in (psutil.net_if_addrs() or {}).values():
            for e in entries or []:
                if int(getattr(e, "family", -1)) != int(getattr(socket, "AF_INET", 2)):
                    continue
                b = str(getattr(e, "broadcast", "") or "").strip()
                if b:
                    out.add(b)
    except Exception:
        swallow.note('mesh_discovery._broadcast_targets:L193')
        pass
    return [(b, port) for b in sorted(out)]


# ── Пакет ─────────────────────────────────────────────────────────────────

def _identity():
    try:
        import mesh_identity
        return mesh_identity
    except Exception:
        swallow.note('mesh_discovery._identity:L205')
        return None


def announce_text(group, url, ts, nonce):
    """Каноническая строка анонса. Собирается одной функцией, чтобы отправитель
    и получатель не разошлись в порядке полей."""
    return "|".join([MAGIC, str(group), str(url), str(int(ts)), str(nonce)])


def _announce_packet(group):
    """Собрать анонс. Полей ровно столько, сколько перечислено — гейт сверяет
    список и не даст протащить в сеть пользовательские данные."""
    ts = int(time.time())
    nonce = ("%08x" % (int.from_bytes(os.urandom(4), "big")))
    pkt = {"v": 1, "m": MAGIC, "grp": group, "url": own_url(), "ts": ts,
           "n": nonce, "name": socket.gethostname()[:40]}
    mi = _identity()
    if mi is not None and mi.enabled():
        try:
            rec = mi.ensure_node()
        except Exception:
            swallow.note('mesh_discovery._announce_packet:L227')
            rec = None
        if rec:
            pkt["nid"] = rec["node_id"]
            pkt["pub"] = base64.urlsafe_b64encode(
                rec["public_pem"].encode()).decode()
            sig = mi.sign_text(announce_text(group, pkt["url"], ts, nonce))
            if sig:
                pkt["sig"] = sig
    return pkt


def _verify_announce(pkt, group):
    """Проверить анонс соседа. Возвращает (ok, node, public_pem, verified, why).

    Причина нужна не панели, а честности: «подпись не сошлась» и «часы разъехались»
    — разные неисправности, и молча сводить их к одной было бы ловушкой.
    """
    if not isinstance(pkt, dict):
        return False, "", None, False, "не пакет"
    if str(pkt.get("m") or "") != MAGIC or int(pkt.get("v") or 0) != 1:
        return False, "", None, False, "не наш протокол"
    if str(pkt.get("grp") or "") != str(group):
        return False, "", None, False, "другая группа"
    url = str(pkt.get("url") or "")
    try:
        ts = int(pkt.get("ts"))
    except Exception:
        swallow.note('mesh_discovery._verify_announce:L255')
        return False, "", None, False, "нет метки времени"
    if abs(time.time() - ts) > TS_SKEW_S:
        return False, "", None, False, "часы разошлись больше чем на %d с" % TS_SKEW_S
    nonce = str(pkt.get("n") or "")
    if not nonce or len(nonce) > 32:
        return False, "", None, False, "одноразовое число не годится"
    sig = str(pkt.get("sig") or "")
    pub_b64 = str(pkt.get("pub") or "")
    nid = str(pkt.get("nid") or "")
    if not sig or not pub_b64:
        # Анонс без личности:candidate записываем, но ключ не закрепляем и
        # в панели он помечен «не подтверждён».
        return True, nid, None, False, ""
    try:
        pub = base64.urlsafe_b64decode(pub_b64.encode()).decode()
    except Exception:
        swallow.note('mesh_discovery._verify_announce:L272')
        return False, "", None, False, "ключ не читается"
    if hashlib.sha256(pub.encode()).hexdigest()[:16] != nid:
        return False, "", None, False, "идентификатор узла не совпал с ключом"
    mi = _identity()
    if mi is None:
        return False, "", None, False, "проверка подписи недоступна"
    text = announce_text(group, url, ts, nonce)
    if not mi.verify_signature(pub, text, sig):
        return False, "", None, False, "подпись не сошлась"
    return True, nid, pub, True, ""


def _is_own(pkt):
    """Свой же анонс возвращается бродкастом: принимать его за соседа нельзя."""
    url = str((pkt or {}).get("url") or "")
    if url and url == own_url():
        return True
    mi = _identity()
    if mi is not None:
        try:
            mine = mi.node_id()
        except Exception:
            swallow.note('mesh_discovery._is_own:L295')
            mine = None
        if mine and str((pkt or {}).get("nid") or "") == mine:
            return True
    return False


# ── Кандидаты ─────────────────────────────────────────────────────────────

def _load_cand():
    """Вызывать под _lock. Диск — только чтобы список не исчезал за секунду
    после перезапуска; живой смысл имеет last_seen, просроченное отсеивается."""
    if _cand["loaded"]:
        return _cand["rec"]
    _cand["loaded"] = True
    out = {}
    try:
        with open(DISCOVERY_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("nodes"), dict):
            for k, v in raw["nodes"].items():
                if isinstance(v, dict) and v.get("url"):
                    out[str(k)[:40]] = v
    except Exception:
        swallow.note('mesh_discovery._load_cand:L319')
        out = {}
    _cand["rec"] = out
    return out


def _save_cand():
    """Вызывать под _lock."""
    _atomic_write(DISCOVERY_PATH, {"nodes": _cand["rec"],
                                   "saved_at": round(time.time(), 3)})


def _atomic_write(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.debug("[discovery] запись %s: %s", path, e)
        return False


def _rejected_src(addr):
    """Анонс извне приватной сети не принимаем: сосед по grid обязан быть
    адресом организации, а «кандидат», которого нельзя легально добавить,
    был бы ловушкой в панели."""
    try:
        ip = ipaddress.ip_address(str(addr or ""))
    except ValueError:
        swallow.note('mesh_discovery._rejected_src:L353')
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


def _reject(why):
    """Запомнить причину отказа: «список пуст» и «пуст, потому что всё
    отвергается по одной причине» — для админа это разные ситуации."""
    with _lock:
        _run["rejects"] = int(_run.get("rejects") or 0) + 1
        _run["last_reject"] = why
    return {"ok": False, "why": why}


def note_announce(pkt, addr=None, group=None):
    """Записать услышанный анонс. Возвращает dict с решением — по нему гейт
    судит, что произошло, и панели есть что показать."""
    if not isinstance(pkt, dict):
        return _reject("не пакет")
    if _is_own(pkt):
        # Свой пакет, вернувшийся бродкастом, — не отказ и не событие.
        return {"ok": False, "why": "свой анонс", "own": True}
    if _rejected_src(addr):
        return _reject("источник вне локальной сети")
    g = str(group or cfg()["group"])
    if str(pkt.get("m") or "") != MAGIC or int(pkt.get("v") or 0) != 1:
        return _reject("не наш протокол")
    if str(pkt.get("grp") or "") != g:
        return _reject("другая группа")
    import client_offload
    url = client_offload.clean_peer_url(str(pkt.get("url") or ""))
    if not url:
        return _reject("адрес не прошёл проверку соседей")
    ok, nid, pub, verified, why = _verify_announce(pkt, g)
    if not ok:
        return _reject(why)
    if not nid:
        nid = "u_" + hashlib.sha256(url.encode()).hexdigest()[:16]
    now = time.time()
    with _lock:
        rec = _load_cand()
        old = rec.get(nid) or {}
        if pub is not None:
            mi = _identity()
            if mi is not None:
                try:
                    mi._pin_peer(nid, pub, url)
                except Exception:
                    swallow.note('mesh_discovery.note_announce:L401')
                    pass
        ent = {"node": nid, "url": url, "name": str(pkt.get("name") or "")[:40],
               "verified": bool(verified), "first_seen": float(old.get("first_seen") or now),
               "last_seen": now, "seen": int(old.get("seen") or 0) + 1,
               # Ключ уже закреплялся за этим узлом раньше: пустая подпись в
               # свежем анонсе (личность выключили) не должна стирать отпечаток.
               "fp": _fp(pub) or str(old.get("fp") or ""),
               "added_at": float(old.get("added_at") or 0)}
        rec[nid] = ent
        if len(rec) > MAX_CANDIDATES:
            for k in sorted(rec, key=lambda k: rec[k].get("last_seen") or 0)[:-MAX_CANDIDATES]:
                rec.pop(k, None)
        _save_cand()
    return {"ok": True, "node": nid, "url": url, "verified": bool(verified)}


def _fp(pub):
    if not pub:
        return ""
    return ":".join(hashlib.sha256(pub.encode()).hexdigest()[i:i + 4]
                    for i in (0, 4, 8, 12))


def candidates(ttl_s=None, include_stale=False):
    """Список найденных узлов, свежие первыми. Просроченные по умолчанию не
    показываются: «сосед» — это тот, кто ещё объявляется."""
    t = int(ttl_s or cfg()["ttl_s"])
    now = time.time()
    out = []
    with _lock:
        for rec in _load_cand().values():
            age = now - float(rec.get("last_seen") or 0)
            if age > t and not include_stale:
                continue
            out.append({"node": str(rec.get("node") or "")[:40],
                        "url": str(rec.get("url") or "")[:200],
                        "name": str(rec.get("name") or "")[:40],
                        "fingerprint": str(rec.get("fp") or "")[:32],
                        "verified": bool(rec.get("verified")),
                        "seen": int(rec.get("seen") or 0),
                        "age_s": round(age, 1),
                        "added_at": float(rec.get("added_at") or 0)})
    out.sort(key=lambda r: r["age_s"])
    return out


def in_grid(url):
    try:
        import client_offload
        peers = (client_offload.decentral().get("grid") or {}).get("peers") or []
    except Exception:
        swallow.note('mesh_discovery.in_grid:L453')
        peers = []
    return url in [str(p) for p in peers]


def mark_added(node):
    with _lock:
        rec = _load_cand().get(node)
        if not rec:
            return False
        rec["added_at"] = round(time.time(), 3)
        _save_cand()
        return True


def forget(node):
    """Убрать кандидата из списка. Адрес из grid.peers эта функция не трогает:
    отзыв соседа — отдельное действие администратора."""
    with _lock:
        rec = _load_cand()
        if node not in rec:
            return False
        rec.pop(node, None)
        _save_cand()
        return True


def purge():
    """Стоп-кран: список кандидатов с диска тоже уходит."""
    with _lock:
        _cand["rec"], _cand["loaded"] = {}, True
        _atomic_write(DISCOVERY_PATH, {"nodes": {}, "purged_at":
                                       round(time.time(), 3)})
    return True


# ── Поток анонсов и приёма ────────────────────────────────────────────────

def _make_sock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # На Windows SO_REUSEADDR разрешил бы второму процессу привязаться к тому
    # же порту и молча забрать чужие анонсы. Эксклюзивный bind вместо этого
    # даёт явную ошибку, и панель показывает её строкой «порт занят».
    excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if excl is not None:
        s.setsockopt(socket.SOL_SOCKET, excl, 1)
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("0.0.0.0", port))
    s.settimeout(0.5)
    return s


def _loop(port, group, interval_s):
    try:
        sock = _make_sock(port)
    except Exception as e:
        with _lock:
            _run["error"] = "порт %d занят другим процессом: %s" % (port, e)
            _run["running"], _run["sock"], _run["thread"] = False, None, None
        logger.warning("[discovery] анонсы не запущены: %s", e)
        return
    with _lock:
        _run["sock"], _run["running"], _run["error"] = sock, True, None
        _run["started_at"], _run["last_send_at"] = time.time(), 0.0
    nxt = 0.0
    buf = bytearray(8192)
    while not _run["stop"]:
        now = time.monotonic()
        if now >= nxt:
            try:
                pkt = _announce_packet(group)
                raw = json.dumps(pkt, ensure_ascii=False).encode()
                for tgt in _broadcast_targets(port):
                    try:
                        sock.sendto(raw, tgt)
                    except OSError:
                        swallow.note('mesh_discovery._loop:L531')
                        pass
                with _lock:
                    _run["sent"] += 1
                    _run["last_send_at"] = time.time()
            except Exception as e:
                logger.debug("[discovery] анонс: %s", e)
            nxt = now + interval_s
        try:
            data, addr = sock.recvfrom_into(buf)
        except socket.timeout:
            swallow.note('mesh_discovery._loop:L542')
            continue
        except OSError:
            swallow.note('mesh_discovery._loop:L545')
            break
        if not data:
            continue
        try:
            pkt = json.loads(bytes(buf[:data]).decode("utf-8", "replace"))
        except Exception:
            swallow.note('mesh_discovery._loop:L552')
            continue
        with _lock:
            _run["recv"] += 1
            _run["last_recv_at"] = time.time()
        try:
            note_announce(pkt, addr=addr[0], group=group)
        except Exception as e:
            logger.debug("[discovery] приём: %s", e)
    try:
        sock.close()
    except Exception:
        swallow.note('mesh_discovery._loop:L564')
        pass
    with _lock:
        _run["running"], _run["sock"], _run["thread"] = False, None, None


def start(force=False):
    """Поднять анонсы. Выключенный флаг — ровно None, и сокет не создаётся.

    Смена порта или имени сети при уже живом потоке требует переподключения:
    иначе «включено и слушает 8445» означало бы, что слушается старый порт,
    а новый админ видит в панели цифры, которые к нему отношения не имеют.
    """
    if not (enabled() or force):
        return None
    c = cfg()
    with _lock:
        alive = bool(_run["thread"] and _run["thread"].is_alive())
        same = (_run["running"] and _run["port"] == c["port"]
                and _run["group"] == c["group"])
    if alive and same and not force:
        return _run["thread"]
    if alive:
        stop()
    with _lock:
        _run["stop"] = False
        _run["port"], _run["group"] = c["port"], c["group"]
        t = threading.Thread(target=_loop,
                             args=(c["port"], c["group"], c["interval_s"]),
                             name="mesh-discovery", daemon=True)
        _run["thread"] = t
        t.start()
        return t


def stop():
    with _lock:
        _run["stop"] = True
        t = _run["thread"]
    if t:
        try:
            t.join(timeout=2.0)
        except Exception:
            swallow.note('mesh_discovery.stop:L607')
            pass
    with _lock:
        _run["thread"], _run["running"], _run["sock"] = None, False, None
    return True


def apply_state():
    """Привести поток в соответствие флагу — вызывается после сохранения
    конфига, чтобы рубильник работал без перезапуска."""
    if enabled():
        start()
    else:
        stop()
    return status()


def running():
    return bool(_run["running"])


def status():
    c = cfg()
    mi = _identity()
    mine = None
    if mi is not None and mi.enabled():
        try:
            mine = {"node_id": mi.node_id(),
                    "fingerprint": _fp(mi.public_pem())}
        except Exception:
            swallow.note('mesh_discovery.status:L637')
            mine = None
    with _lock:
        return {"enabled": bool(c["enabled"]), "running": bool(_run["running"]),
                "port": int(_run["port"] or c["port"]), "group": c["group"],
                "interval_s": c["interval_s"], "ttl_s": c["ttl_s"],
                "own_url": own_url() if c["enabled"] else "",
                "identity": bool(mine), "node": mine or {},
                "sent": int(_run["sent"]), "recv": int(_run["recv"]),
                "rejected": int(_run.get("rejects") or 0),
                "last_reject": str(_run.get("last_reject") or "")[:80],
                "error": _run["error"],
                "since": round(_run["started_at"], 3) if _run["running"] else 0,
                # При выключенном флаге панель не должна показывать список:
                # строки, услышанные вчера, выглядели бы как живые соседи.
                "candidates": candidates(c["ttl_s"]) if c["enabled"] else []}


def summary():
    """Одна строка для панели: включено ли и что видно."""
    st = status()
    if not st["enabled"]:
        return "выключено — анонсов нет"
    if not st["running"]:
        return st["error"] or "включено, но поток не поднялся"
    return "слушает %d · найдено %d" % (st["port"], len(st["candidates"]))


def reset_for_tests():
    with _lock:
        _cand["rec"], _cand["loaded"] = {}, False
        _run["sent"] = _run["recv"] = 0
        _run["rejects"], _run["last_reject"] = 0, ""
        _run["error"] = None
        _selftest["url"] = None
