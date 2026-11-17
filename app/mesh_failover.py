"""Перехват роли сервера: чтение у соседа, когда свой сервер не поднялся.

Выбор узла делает оболочка (desktop/main.js): когда свой сервер мёртв,
спросить его о кандидатах нельзя по определению, поэтому оболочка читает те
же два файла, что и этот модуль — анонсы (mesh_discovery.json) и
подтверждённых соседей (mesh_peers.json).

Здесь живёт вторая половина механизма:

* `status()` — что увидит панель: кто может отдать данные по выбранному
  правилу доверия и кого выбрали бы сейчас;
* `blocked()` — настоящий серверный запрет записи. Оболочка помечает все
  запросы к соседу заголовком, и сервер соседа (этой же сборки) отклоняет
  изменяющие методы. Спрятать кнопки в чужом интерфейсе было бы обещанием,
  а не запретом.

Заголовок — не защита от злого соседа (его можно не прислать). Он закрывает
другую дыру: пользователь в режиме перехвата не должен править данные,
которые ему не принадлежат, даже случайно.
"""
import json
import os
import time
import swallow

HEADER_READONLY = "x-infinity-readonly"
READ_METHODS = ("GET", "HEAD", "OPTIONS")


def _data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('mesh_failover._data_dir:L37')
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load_json(name, key):
    path = os.path.join(_data_dir(), name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        swallow.note('mesh_failover._load_json:L47')
        return {}
    if isinstance(raw, dict) and isinstance(raw.get(key), dict):
        return raw[key]
    return {}


def _mesh():
    try:
        import client_offload
        return client_offload.decentral().get("mesh") or {}
    except Exception:
        swallow.note('mesh_failover._mesh:L59')
        return {}


def enabled():
    return bool(_mesh().get("failover"))


def trust():
    mode = str(_mesh().get("failover_trust") or "").strip().lower()
    return mode if mode in ("verified", "any", "manual") else "verified"


def manual_node():
    return str(_mesh().get("failover_node") or "").strip()[:120]


def candidates():
    """Кандидаты из анонсов и подтверждённых соседей — по одному на адрес.

    `verified` означает «подпись анонса проверена»; узел из mesh_peers.json
    подтверждён по определению — туда попадают только после проверки.
    """
    disc = _load_json("mesh_discovery.json", "nodes")
    pinned = _load_json("mesh_peers.json", "peers")
    out = {}
    for node, rec in disc.items():
        if not isinstance(rec, dict) or not rec.get("url"):
            continue
        key = str(rec.get("url"))
        out[key] = {"node": str(node)[:40], "url": key,
                    "verified": bool(rec.get("verified")),
                    "pinned": str(node) in pinned,
                    "fingerprint": str(rec.get("fp") or "")[:32],
                    "source": "announce",
                    "seen": float(rec.get("last_seen") or 0)}
    for node, rec in pinned.items():
        if not isinstance(rec, dict) or not rec.get("url"):
            continue
        key = str(rec.get("url"))[:200]
        item = out.get(key) or {"node": str(node)[:40], "url": key,
                                "verified": True, "fingerprint": "",
                                "source": "peer", "seen": 0.0}
        item["pinned"] = True
        item["verified"] = True
        out[key] = item
    return sorted(out.values(), key=lambda c: (-int(bool(c["pinned"])),
                                               -int(bool(c["verified"])),
                                               -c["seen"], c["url"]))


def pick(items=None):
    """Кого выберет правило доверия. None — подходящих нет."""
    items = candidates() if items is None else items
    mode = trust()
    if mode == "manual":
        want = manual_node()
        if not want:
            return None
        for c in items:
            if want in (c["node"], c["url"]):
                return c
        return None
    if mode == "verified":
        for c in items:
            if c["verified"] or c["pinned"]:
                return c
        return None
    return items[0] if items else None


def blocked(method, headers):
    """Изменяющий запрос в режиме только-чтения — отказ."""
    if str(method or "").upper() in READ_METHODS:
        return False
    try:
        raw = str((headers or {}).get(HEADER_READONLY) or "").strip().lower()
    except Exception:
        swallow.note('mesh_failover.blocked:L137')
        return False
    return raw in ("1", "true", "yes", "on")


class ReadonlyGuard:
    """ASGI-сторож запрета записи.

    Чистый ASGI, а не BaseHTTPMiddleware: тот оборачивает тело ответа и
    вмешивается в стриминг (SSE, длинные ответы). Здесь отказ отдаётся
    отдельным ответом, а всё остальное уходит дальше без обёртки.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = {}
            for k, v in scope.get("headers") or []:
                headers[k.decode("latin-1").lower()] = v.decode("latin-1")
            if blocked(scope.get("method"), headers):
                body = json.dumps({"error": "Этот узел отдаёт данные только на "
                                            "чтение: запись здесь запрещена"},
                                  ensure_ascii=False).encode("utf-8")
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type",
                                         b"application/json; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def status():
    items = candidates()
    chosen = pick(items)
    mode = trust()
    if not enabled():
        note = ("перехват выключен: когда свой сервер не поднимется, оболочка "
                "покажет прежнюю страницу ошибки")
    elif chosen is None:
        if mode == "manual" and not manual_node():
            note = "выбран ручной узел, но он не указан"
        elif mode == "manual":
            note = "указанный узел не найден среди анонсов и соседей"
        elif not items:
            note = "кандидатов нет: включите поиск узлов или добавьте соседа"
        else:
            note = "нет кандидатов с проверенной подписью"
    else:
        note = "при падении своего сервера оболочка откроет %s (%s)" % (
            chosen["url"], chosen["node"])
    return {"enabled": enabled(), "trust": mode, "manual_node": manual_node(),
            "chosen": chosen, "count": len(items), "note": note,
            "header": HEADER_READONLY,
            "items": [{"node": c["node"], "url": c["url"],
                       "verified": c["verified"], "pinned": c["pinned"],
                       "source": c["source"],
                       "age_s": None if not c["seen"] else round(time.time() - c["seen"], 1)}
                      for c in items[:16]]}
