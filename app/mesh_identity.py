#!/usr/bin/env python3
"""mesh_identity — кто этот узел и кто к нему подключается.

Флаг decentral.mesh.identity до этой версии читался только санитайзером
конфига: ни ключа, ни проверки не существовало. Здесь он становится
настоящим, на двух уровнях, как и было решено:

  * identity выключен (по умолчанию) — ровно прежнее поведение. Ни одного
    лишнего сообщения в сокете помощника, ни одной лишней подписи в запросах
    к соседям, ни одной отказа по подписи;
  * identity включён, required выключен — сервер заводит ключ узла, выдаёт
    помощнику секрет и просит его подтвердить, но никого не отсеивает:
    неподтверждённое устройство продолжает работать, просто помечается
    «не проверено»;
  * required включён — неподтверждённое подключение не регистрируется.

Секреты помощника и приватный ключ узла хранятся в data/ и ни одним
эндпоинтом не отдаются: в панели видны идентификатор, состояние и отпечаток
ключа, но ни байта тайны.

Для браузера асимметричная криптография не годится: crypto.subtle нет в
незащищённом контексте (http://192.168.x.x — обычный режим этого приложения),
а помощник живёт именно там. Поэтому помощнику выдаётся HMAC-секрет (TOFU:
первое соединение после одобрения), а узел к узлу — Ed25519-подпись запроса:
там python и cryptography всегда под рукой.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import swallow

logger = logging.getLogger("infinity")

CHALLENGE_TTL_S = 30.0
MAX_PENDING = 512
MAX_SECRET_FAILS = 3
PEER_SKEW_S = 300.0
REPLAY_WINDOW_S = 600.0
MAX_REPLAY = 4096
HEADER_NODE = "x-mesh-node"
HEADER_TS = "x-mesh-ts"
HEADER_NONCE = "x-mesh-nonce"
HEADER_SIG = "x-mesh-sig"
HEADER_PUB = "x-mesh-pub"


def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('mesh_identity._data_dir:L61')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


NODE_PATH = os.path.join(_data_dir(), "mesh_node.json")
HELPERS_PATH = os.path.join(_data_dir(), "mesh_helpers.json")
PEERS_PATH = os.path.join(_data_dir(), "mesh_peers.json")

_lock = threading.RLock()
_node = {"loaded": False, "rec": None}
_helpers = {"loaded": False, "rec": {}}
_peers = {"loaded": False, "rec": {}, "rev": {}, "url": {}}
_pending = {}       # device_id -> {"nonce", "at", "tries"}
_seen_nonces = {}   # "%s|%s" % (node, nonce) -> at
_crypto = {"tried": False, "ok": False}


# ── Флаг ──────────────────────────────────────────────────────────────────

def _mesh_cfg():
    try:
        import client_offload
        return (client_offload.decentral().get("mesh") or {})
    except Exception:
        swallow.note('mesh_identity._mesh_cfg:L85')
        return {}


def enabled():
    return bool(_mesh_cfg().get("identity"))


def required():
    """Отсеивать ли неподтверждённых. Без включённого identity не отсеивает
    ничего: required сам по себе ничего не означает."""
    m = _mesh_cfg()
    return bool(m.get("identity")) and bool(m.get("identity_required"))


# ── Ключ узла ─────────────────────────────────────────────────────────────

def crypto_available():
    if not _crypto["tried"]:
        _crypto["tried"] = True
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: F401
            _crypto["ok"] = True
        except Exception:
            _crypto["ok"] = False
            logger.info("[mesh] cryptography недоступна — подпись узлов не ставится")
    return bool(_crypto["ok"])


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
        logger.debug("[mesh] запись %s: %s", path, e)
        return False


def _fingerprint(public_pem):
    return ":".join(hashlib.sha256((public_pem or "").encode()).hexdigest()[i:i + 4]
                    for i in (0, 4, 8, 12))


def _new_node():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    return {"node_id": hashlib.sha256(pub.encode()).hexdigest()[:16],
            "algo": "ed25519", "public_pem": pub, "private_pem": priv,
            "created_at": round(time.time(), 3)}


def ensure_node():
    """Ключ узла создаётся один раз и переживает перезапуск: без этого
    закрепление соседского ключа теряло бы смысл при каждом старте."""
    with _lock:
        if _node["loaded"]:
            return _node["rec"]
        _node["loaded"] = True
        rec = None
        try:
            with open(NODE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if (isinstance(raw, dict) and raw.get("node_id")
                    and raw.get("public_pem") and raw.get("private_pem")):
                rec = raw
        except Exception:
            swallow.note('mesh_identity.ensure_node:L164')
            rec = None
        if rec is None:
            if not crypto_available():
                _node["rec"] = None
                return None
            rec = _new_node()
            _atomic_write(NODE_PATH, rec)
        _node["rec"] = rec
    return rec


def node_id():
    rec = ensure_node()
    return (rec or {}).get("node_id")


def public_pem():
    rec = ensure_node()
    return (rec or {}).get("public_pem")


def sign_text(text):
    rec = ensure_node()
    if not rec:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(rec["private_pem"].encode(), None)
        return key.sign(str(text).encode()).hex()
    except Exception as e:
        logger.debug("[mesh] подпись: %s", e)
        return None


def verify_signature(public_pem, text, sig_hex):
    if not public_pem or not sig_hex:
        return False
    try:
        from cryptography.hazmat.primitives import serialization
        pub = serialization.load_pem_public_key(str(public_pem).encode())
        pub.verify(bytes.fromhex(str(sig_hex)), str(text).encode())
        return True
    except Exception:
        swallow.note('mesh_identity.verify_signature:L208')
        return False


# ── Запросы к соседям ─────────────────────────────────────────────────────

def canonical_query(params):
    """Сосед обязан собрать ту же строку, что и мы, поэтому порядок
    однозначный: по ключу, без пробелов, None — пустое значение."""
    items = sorted((str(k), "" if v is None else str(v)) for k, v in (params or {}).items())
    return "&".join("%s=%s" % kv for kv in items)


def canonical_request(method, path, params, ts, nonce):
    return "|".join([str(method).upper(), str(path or "/"), canonical_query(params),
                     str(ts), str(nonce)])


def peer_headers(method, path, params=None):
    """Заголовки подписанного запроса к соседней ноде.

    Пустой словарь при выключенном флаге означает «запрос ровно такой, каким
    был всегда» — это и есть обещание отсутствия регресса."""
    if not enabled():
        return {}
    rec = ensure_node()
    if not rec:
        return {}
    ts = int(time.time())
    nonce = secrets.token_hex(8)
    msg = canonical_request(method, path, params, ts, nonce)
    sig = sign_text(msg)
    if not sig:
        return {}
    return {HEADER_NODE: rec["node_id"], HEADER_TS: str(ts), HEADER_NONCE: nonce,
            HEADER_SIG: sig,
            HEADER_PUB: base64.urlsafe_b64encode(rec["public_pem"].encode()).decode()}


def _load_peers():
    """Вызывать под _lock."""
    if _peers["loaded"]:
        return _peers["rec"]
    _peers["loaded"] = True
    out, rev = {}, {}
    try:
        with open(PEERS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("peers"), dict):
            out = {str(k)[:40]: v for k, v in raw["peers"].items() if isinstance(v, dict)}
        # Отозванные узлы живут в том же файле: отзыв без следа означал бы,
        # что следующий же подписанный запрос закрепится заново по TOFU.
        if isinstance(raw, dict) and isinstance(raw.get("revoked"), dict):
            rev = {str(k)[:40]: v for k, v in raw["revoked"].items()
                   if isinstance(v, dict)}
    except Exception:
        swallow.note('mesh_identity._load_peers:L264')
        out = {}
    _peers["rec"] = out
    _peers["rev"] = rev
    return out


def _save_peers():
    """Вызывать под _lock."""
    _atomic_write(PEERS_PATH, {"peers": _peers["rec"], "revoked": _peers["rev"]})


def _pin_peer(node, public_pem, url=None):
    with _lock:
        _load_peers()
        now = time.time()
        rec = _peers["rec"].get(node) or {"first_seen": now, "node": node}
        rec.update({"public_pem": public_pem, "last_seen": now,
                    "url": str(url or _peers["url"].get(node)
                               or rec.get("url") or "")[:200]})
        _peers["rec"][node] = rec
        _save_peers()
        return rec


def note_peer_url(node, url):
    """Запомнить, по какому адресу живёт узел с таким id.

    Закрепление рождается из входящего запроса, а адрес сосед видит только
    тогда, когда тянет у него сам: без этой подписки колонка адреса в панели
    осталась бы пустой, а отзыв по адресу не нашёл бы чужой ключ."""
    node = str(node or "")[:40]
    url = str(url or "")[:200]
    if not node or not url:
        return False
    with _lock:
        _load_peers()
        _peers["url"][node] = url
        rec = _peers["rec"].get(node)
        changed = bool(rec) and rec.get("url") != url
        if changed:
            rec["url"] = url
        for r in _peers["rev"].values():
            if r.get("node") == node:
                r["url"] = url
        if changed:
            _save_peers()
        return True


def pinned_peers():
    with _lock:
        _load_peers()
        return {k: {"node": k, "fingerprint": _fingerprint(v.get("public_pem")),
                    "first_seen": v.get("first_seen"), "last_seen": v.get("last_seen"),
                    "url": v.get("url") or "", "ok": bool(v.get("public_pem"))}
                for k, v in _peers["rec"].items()}


def revoked_peers():
    """Список отозванных узлов: отзыв обязан пережить следующий запрос,
    иначе «забыть ключ» означает «забыть до первого касания»."""
    with _lock:
        _load_peers()
        return {k: {"node": k, "fingerprint": v.get("fingerprint") or "",
                    "at": v.get("at"), "url": v.get("url") or ""}
                for k, v in _peers["rev"].items()}


def revoke_peer(node):
    with _lock:
        _load_peers()
        key = str(node or "")[:40]
        had = _peers["rec"].pop(key, None)
        if had is None:
            return False
        _peers["rev"][key] = {"node": key,
                              "fingerprint": _fingerprint(had.get("public_pem")),
                              "url": str(had.get("url") or "")[:200],
                              "at": round(time.time(), 3)}
        _save_peers()
        return True


def unrevoke_peer(node=None, url=None):
    """Вернуть довериям метку: явно по id узла или по адресу, который админ
    вернул в сетку. Без этого отзыва был бы точкой невозврата."""
    node = str(node or "")[:40]
    url = str(url or "").rstrip("/")[:200]
    back = []
    with _lock:
        _load_peers()
        if node and node in _peers["rev"]:
            _peers["rev"].pop(node, None)
            back.append(node)
        if url:
            for k in [k for k, v in _peers["rev"].items()
                      if str(v.get("url") or "").rstrip("/") == url]:
                _peers["rev"].pop(k, None)
                back.append(k)
        if back:
            _save_peers()
    return back


def _trim_replay(now):
    stale = [k for k, at in _seen_nonces.items() if now - at > REPLAY_WINDOW_S]
    for k in stale:
        _seen_nonces.pop(k, None)
    while len(_seen_nonces) > MAX_REPLAY:
        _seen_nonces.pop(next(iter(_seen_nonces)))


def verify_peer_request(method, path, params, headers):
    """Проверить подпись соседнего запроса.

    Возвращает вердикт, но не отменяет запрос: решение «отказывать или нет»
    принимает вызывающая сторона, потому что required — её настройка.

    Первый контакт закрепляет ключ соседа (TOFU): последующие подписи тем же
    идентификатором, но другим ключом — это подмена, и она не проходит.
    """
    h = {}
    for k, v in (headers or {}).items():
        h[str(k).lower()] = str(v or "")
    if not h.get(HEADER_SIG):
        return {"ok": False, "reason": "нет подписи", "node": h.get(HEADER_NODE) or ""}
    if not enabled():
        # Флаг выключен: проверять нечем и незачем, вердикт ни на что не влияет.
        return {"ok": False, "reason": "mesh.identity выключен", "node": h.get(HEADER_NODE) or ""}
    node = h.get(HEADER_NODE) or ""
    try:
        pub = base64.urlsafe_b64decode(h.get(HEADER_PUB).encode()).decode()
    except Exception:
        swallow.note('mesh_identity.verify_peer_request:L398')
        return {"ok": False, "reason": "ключ не читается", "node": node}
    if hashlib.sha256((pub or "").encode()).hexdigest()[:16] != node:
        return {"ok": False, "reason": "идентификатор не совпадает с ключом", "node": node}
    try:
        ts = int(h.get(HEADER_TS))
    except Exception:
        swallow.note('mesh_identity.verify_peer_request:L405')
        return {"ok": False, "reason": "метка времени не читается", "node": node}
    now = time.time()
    if abs(now - ts) > PEER_SKEW_S:
        return {"ok": False, "reason": "расхождение часов больше %d с" % PEER_SKEW_S,
                "node": node}
    nonce = (h.get(HEADER_NONCE) or "")[:64]
    if len(nonce) < 8:
        return {"ok": False, "reason": "одноразовое число слишком короткое", "node": node}
    msg = canonical_request(method, path, params, ts, nonce)
    if not verify_signature(pub, msg, h.get(HEADER_SIG)):
        return {"ok": False, "reason": "подпись не проходит", "node": node}
    key = "%s|%s" % (node, nonce)
    with _lock:
        _trim_replay(now)
        if key in _seen_nonces:
            return {"ok": False, "reason": "повтор запроса (replay)", "node": node}
        _seen_nonces[key] = now
    pinned = pinned_peers().get(node)
    if pinned and pinned["fingerprint"] != _fingerprint(pub):
        return {"ok": False, "reason": "ключ соседа не совпадает с закреплённым",
                "node": node}
    if node in revoked_peers():
        # Отзыв должен пережить следующий запрос: без метки отзывался бы не
        # доступ, а только его запись, и TOFU закреплял соседа заново.
        return {"ok": False, "reason": "узел отозван", "node": node}
    if pinned is None and required():
        # required для соседей означает «незнакомец не получает файлы»: иначе
        # переключатель ничего в их доступе и не менял бы.
        return {"ok": False, "reason": "узел не закреплён, а требуется закрепление",
                "node": node, "new": True}
    fresh = pinned is None
    _pin_peer(node, pub)
    return {"ok": True, "reason": "первый контакт" if fresh else "ключ закреплён",
            "node": node, "fingerprint": _fingerprint(pub), "new": fresh}


# ── Помощник: секрет и подтверждение ──────────────────────────────────────

def _load_helpers():
    if _helpers["loaded"]:
        return _helpers["rec"]
    _helpers["loaded"] = True
    out = {}
    try:
        with open(HELPERS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and isinstance(raw.get("helpers"), dict):
            out = {str(k)[:80]: v for k, v in raw["helpers"].items() if isinstance(v, dict)}
    except Exception:
        swallow.note('mesh_identity._load_helpers:L455')
        out = {}
    _helpers["rec"] = out
    return out


def _save_helpers():
    _atomic_write(HELPERS_PATH, {"helpers": _helpers["rec"]})


def _proof_message(device_id, nonce):
    return "%s|%s" % (device_id, nonce)


def compute_proof(secret, device_id, nonce):
    """То же, что считает браузер на своём конце (HMAC-SHA256, hex)."""
    return hmac.new(str(secret or "").encode(),
                    _proof_message(device_id, nonce).encode(),
                    hashlib.sha256).hexdigest()


def issue_secret(device_id):
    """Выдать секрет помощнику. Новый — только если его ещё нет или он
    отозван: перевыпуск на каждое переподключение обнулил бы смысл
    подтверждения."""
    did = str(device_id or "")[:80]
    if not did:
        return None
    with _lock:
        rec = _load_helpers()
        cur = rec.get(did) or {}
        if cur.get("secret") and cur.get("state") != "revoked":
            return cur["secret"]
        rec[did] = {"secret": secrets.token_hex(32), "state": "issued",
                    "issued_at": round(time.time(), 3), "fails": 0,
                    "verified_at": None}
        _save_helpers()
        return rec[did]["secret"]


def rotate_secret(device_id):
    did = str(device_id or "")[:80]
    with _lock:
        rec = _load_helpers()
        rec[did] = {"secret": secrets.token_hex(32), "state": "issued",
                    "issued_at": round(time.time(), 3), "fails": 0,
                    "verified_at": None}
        _save_helpers()
        return rec[did]["secret"]


def drop_secret(device_id):
    """Отозвать секрет: состояние revoked, ключ стёрт. Перезапросить его
    устройство сможет только пройдя регистрацию заново."""
    did = str(device_id or "")[:80]
    with _lock:
        rec = _load_helpers()
        cur = rec.get(did)
        if not cur:
            return False
        cur.update({"secret": None, "state": "revoked", "revoked_at": round(time.time(), 3)})
        _save_helpers()
        return True


def secret_of(device_id):
    with _lock:
        rec = _load_helpers().get(str(device_id or "")[:80]) or {}
        return rec.get("secret")


def helper_state(device_id):
    with _lock:
        rec = _load_helpers().get(str(device_id or "")[:80])
    if not rec:
        return "unsigned"
    return rec.get("state") or "unsigned"


def begin_challenge(device_id):
    """Одноразовое число на подтверждение. None — когда подтверждение не
    нужно (флаг выключен) или секрет ещё не выдан."""
    if not enabled():
        return None
    did = str(device_id or "")[:80]
    if not did or not secret_of(did):
        return None
    nonce = secrets.token_hex(16)
    with _lock:
        while len(_pending) >= MAX_PENDING:
            _pending.pop(next(iter(_pending)))
        _pending[did] = {"nonce": nonce, "at": time.time(), "tries": 0}
    return nonce


def verify_proof(device_id, nonce, mac):
    did = str(device_id or "")[:80]
    with _lock:
        p = _pending.get(did) or {}
    secret = secret_of(did)
    if not enabled():
        return {"ok": False, "state": helper_state(did), "reason": "mesh.identity выключен"}
    if not secret:
        return {"ok": False, "state": "unsigned", "reason": "секрет не выдан"}
    if not p.get("nonce") or p.get("nonce") != str(nonce or ""):
        return {"ok": False, "state": helper_state(did),
                "reason": "запрос подтверждения истёк или число не то"}
    if time.time() - float(p.get("at") or 0) > CHALLENGE_TTL_S:
        with _lock:
            _pending.pop(did, None)
        return {"ok": False, "state": "expired", "reason": "подтверждение запоздало"}
    ok = False
    try:
        ok = hmac.compare_digest(compute_proof(secret, did, p["nonce"]),
                                 str(mac or "").strip().lower())
    except Exception:
        swallow.note('mesh_identity.verify_proof:L571')
        ok = False
    with _lock:
        _pending.pop(did, None)
        rec = _load_helpers()
        cur = rec.get(did) or {}
        if ok:
            cur.update({"state": "verified", "fails": 0,
                        "verified_at": round(time.time(), 3)})
            rec[did] = cur
            _save_helpers()
            return {"ok": True, "state": "verified", "reason": "устройство подтвердилось"}
        cur["fails"] = int(cur.get("fails") or 0) + 1
        if cur["fails"] >= MAX_SECRET_FAILS:
            # Три подряд неподходящих подписи — это либо чужая попытка, либо
            # сломанный клиент. Секрет снимается, иначе перебор продолжается
            # по одному соединению за раз.
            cur.update({"secret": None, "state": "revoked", "fails": 0,
                        "revoked_at": round(time.time(), 3)})
            reason = "слишком много неверных подтверждений — секрет отозван"
        else:
            cur["state"] = "failed"
            reason = "подпись не совпадает"
        rec[did] = cur
        _save_helpers()
    return {"ok": False, "state": cur.get("state"), "reason": reason}


def purge_all():
    """Стоп-кран: секреты помощников и закреплённые ключи соседов стираются,
    ключ узла остаётся — он ничего никому не отправил.

    Счётчики возвращаются настоящие: панель обязана показать, сколько exactly
    ушло, иначе «стёрто 0» при трёх соседах было бы отчётом о несделанном."""
    with _lock:
        _load_helpers()
        _load_peers()
        counts = {"helpers": len(_helpers["rec"]),
                  "peers": len(_peers["rec"]),
                  "revoked": len(_peers["rev"])}
        _helpers["rec"] = {}
        _helpers["loaded"] = True
        _peers["rec"] = {}
        _peers["rev"] = {}
        _peers["url"] = {}
        _peers["loaded"] = True
        _pending.clear()
        _seen_nonces.clear()
        _atomic_write(HELPERS_PATH, {"helpers": {}})
        _atomic_write(PEERS_PATH, {"peers": {}, "revoked": {}})
    return counts


def helpers_summary():
    with _lock:
        rec = dict(_load_helpers())
    out = {"total": len(rec), "verified": 0, "issued": 0, "failed": 0,
           "revoked": 0, "devices": []}
    for did, r in rec.items():
        st = r.get("state") or "issued"
        if st == "verified":
            out["verified"] += 1
        elif st == "revoked":
            out["revoked"] += 1
        elif st == "failed":
            out["failed"] += 1
        else:
            out["issued"] += 1
        out["devices"].append({"device": did, "state": st, "fails": int(r.get("fails") or 0),
                               "issued_at": r.get("issued_at"),
                               "verified_at": r.get("verified_at"),
                               "has_secret": bool(r.get("secret"))})
    out["devices"].sort(key=lambda x: str(x.get("device")))
    return out


def status():
    rec = ensure_node()
    m = _mesh_cfg()
    with _lock:
        pending = len(_pending)
    return {
        "enabled": bool(m.get("identity")),
        "required": bool(m.get("identity") and m.get("identity_required")),
        "crypto": crypto_available(),
        "node_id": (rec or {}).get("node_id"),
        "algo": (rec or {}).get("algo"),
        "created_at": (rec or {}).get("created_at"),
        "fingerprint": _fingerprint((rec or {}).get("public_pem")),
        "peers": list(pinned_peers().values()),
        # Отозванные отдельно и всегда: панель обязана показывать, что отзыв
        # действует, и давать способ вернуть узел, не переустанавливая приложение.
        "revoked": list(revoked_peers().values()),
        "required_note": ("соседи пускаются только с закреплённым ключом"
                          if (m.get("identity") and m.get("identity_required"))
                          else "первый контакт закрепляет ключ (TOFU)"),
        "helpers": helpers_summary(),
        "pending_challenges": pending,
        "challenge_ttl_s": CHALLENGE_TTL_S,
        "note": ("ключ узла и секреты помощников хранятся в data/ и ни одним "
                 "эндпоинтом не отдаются"),
    }


def reset_for_tests():
    global _crypto
    with _lock:
        _node.update({"loaded": False, "rec": None})
        _helpers.update({"loaded": False, "rec": {}})
        _peers.update({"loaded": False, "rec": {}, "rev": {}, "url": {}})
        _pending.clear()
        _seen_nonces.clear()
        _crypto = {"tried": False, "ok": False}
