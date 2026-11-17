#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HA Web Panel — модуль InfinitySearch: веб-оболочка Home Assistant.

Раньше это был отдельный скрипт (aiohttp + SPA в одном файле), запускаемый
другой программой на другом порту. Теперь это обычный модуль системы:

  * страница   — /ha           (шаблон templates/page_ha.html)
  * вход       — /ha/login
  * REST API   — /ha/api/…
  * WebSocket  — /ha/ws
  * прокси HA  — /ha/api/ha/** → {ha_url}/api/**  (токен HA не покидает сервер)

Мост к Home Assistant (состояния сущностей, вызов служб, история, logbook,
реестры, конфиг автоматизаций) работает через httpx + websockets — те же
зависимости, что и у остальных модулей системы, поэтому в frozen-сборке
ничего доустанавливать не нужно.

Тема: у панели нет собственной палитры — static/css/ha_panel.css мапится
на токены дизайн-системы (static/css/design.css), а переключатель темы
работает через общий InfinityTheme. Поэтому панель всегда следует за темой
приложения и за пользовательским акцентом.

Конфигурация (адрес HA, токен, пароль панели) — ha_panel_config.json
рядом с модулем; файл создаётся с правами 600 и никогда не отдаётся в браузер.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
import swallow

try:
    import httpx
except Exception:      # pragma: no cover - httpx есть в любой сборке
    swallow.note('ha_web_panel_module.<module>:L45')
    httpx = None

logger = logging.getLogger("infinity.ha_panel")

VERSION = "1.1.0"
MODULE_ID = "ha_panel"
BASE_PATH = "/ha"
PAGE_NAME = "page_ha.html"
LOGIN_PAGE_NAME = "page_ha_login.html"

DEFAULTS = {
    "ha_url": "",          # например http://homeassistant.local:8123
    "token": "",           # долгосрочный токен доступа HA
    "app_password": "",    # необязательный пароль входа в панель
    "poll_interval": 5,    # интервал REST-опроса, когда WS недоступен (сек)
}

router = APIRouter(prefix=BASE_PATH, tags=["ha_panel"])


# ═══════════════════════════════ конфигурация ═══════════════════════════════

def legacy_config_path() -> str:
    """Старый общий файл настроек (до разделения по профилям)."""
    base = os.environ.get("INFINITY_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        swallow.note('ha_web_panel_module.legacy_config_path:L74')
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "ha_panel_config.json")


def _default_profile_id() -> str:
    try:
        import profiles_module as PM
        return PM.get_default_profile_id() or "default"
    except Exception:
        swallow.note('ha_web_panel_module._default_profile_id:L84')
        return "default"


def config_path(profile_id: str = "") -> str:
    """Файл настроек панели конкретного профиля.

    Раньше адрес HA, токен и пароль панели были общими на весь экземпляр —
    и профили видели чужой дом. Теперь у каждого профиля свой файл в его
    data-каталоге; старый общий файл один раз копируется default-профилю.
    """
    pid = str(profile_id or "").strip() or _default_profile_id()
    try:
        import profiles_module as PM
        base = os.path.join(PM._profile_data_path(pid), "ha")
    except Exception:
        swallow.note('ha_web_panel_module.config_path:L100')
        base = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "ha_panel_config.json")
    if not os.path.exists(path) and pid == _default_profile_id():
        legacy = legacy_config_path()
        if os.path.isfile(legacy):
            try:
                import shutil
                shutil.copy2(legacy, path)
            except OSError:
                swallow.note('ha_web_panel_module.config_path:L111')
                pass
    return path


def make_sid(password: str, profile_id: str = "") -> str:
    """Подпись cookie-сессии (детерминированная, переживает перезапуск).

    Cookie профиля не должна пускать в панель соседнего профиля с тем же
    паролем, поэтому id входит в подпись. Для default-профиля оставлен
    прежний формат — старые cookie продолжают работать.
    """
    key = hashlib.sha256(("hap-sid::" + password).encode()).digest()
    pid = str(profile_id or "").strip()
    if pid and pid != _default_profile_id():
        msg = ("sid-v1:" + pid).encode()
    else:
        msg = b"sid-v1"
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# ═══════════════════════════════════ мост ══════════════════════════════════

class _Client:
    """Обёртка над WebSocket браузера: сериализует отправку (один поток)."""

    __slots__ = ("ws", "lock")

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.lock = asyncio.Lock()

    async def send_json(self, obj: dict):
        async with self.lock:
            await self.ws.send_json(obj)


class Bridge:
    """Мост между браузерами и Home Assistant: REST-прокси + WS-мост."""

    def __init__(self, cfg_path: str):
        self.cfg_path = cfg_path
        self.cfg = dict(DEFAULTS)
        self.load_cfg()
        self.session = None            # httpx.AsyncClient — обычные запросы
        self.stream_session = None     # httpx.AsyncClient — потоки (камеры)
        self.ws = None                 # соединение с HA WebSocket
        self.connected = False
        self.clients: set = set()      # браузеры (_Client)
        self.pending: dict = {}        # ha_id -> {"ws", "pid", "sub"}
        self._id = 0
        self.gs_id = None              # id команды get_states
        self.sub_id = None             # id подписки на state_changed
        self.states: dict = {}         # entity_id -> state object
        self.started = time.time()
        self.tasks: list = []

    # ─────────────────────────── конфигурация ───────────────────────────
    def load_cfg(self):
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except Exception:
            swallow.note('ha_web_panel_module.load_cfg:L174')
            pass

    def save_cfg(self):
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg_path)
        try:
            os.chmod(self.cfg_path, 0o600)
        except OSError:
            swallow.note('ha_web_panel_module.save_cfg:L185')
            pass

    @property
    def base(self) -> str:
        return (self.cfg.get("ha_url") or "").rstrip("/")

    @property
    def headers(self) -> dict:
        return {"Authorization": "Bearer " + (self.cfg.get("token") or "")}

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    # ─────────────────────────── жизненный цикл ─────────────────────────
    async def start(self):
        if not self.ensure_session():
            logger.warning("[ha] httpx недоступен — мост к Home Assistant выключен")
            return
        self.tasks = [asyncio.create_task(self._ws_loop()),
                      asyncio.create_task(self._poll_loop())]
        logger.info("[ha] Мост Home Assistant запущен. URL: %s",
                    self.base or "(не задан)")

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                swallow.note('ha_web_panel_module.stop:L218')
                pass
            self.ws = None
        for s in (self.session, self.stream_session):
            if s is not None:
                try:
                    await s.aclose()
                except Exception:
                    swallow.note('ha_web_panel_module.stop:L226')
                    pass
        self.session = None
        self.stream_session = None

    async def restart_ws(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                swallow.note('ha_web_panel_module.restart_ws:L236')
                pass
            self.ws = None

    def ensure_session(self) -> bool:
        """Ленивая инициализация клиентов (страница может открыться раньше старта)."""
        if httpx is None:
            return False
        if self.session is None:
            # follow_redirects=True — как в исходной aiohttp-версии: HA иногда
            # отвечает редиректом (например, с /api/ на канонический путь).
            self.session = httpx.AsyncClient(timeout=httpx.Timeout(60.0),
                                             follow_redirects=True)
        if self.stream_session is None:
            self.stream_session = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=None, write=None, pool=None),
                follow_redirects=True)
        return True

    # ─────────────────────── WebSocket-канал к HA ───────────────────────
    async def _ws_loop(self):
        while True:
            try:
                if self.base and self.cfg.get("token"):
                    await self._ws_connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[ha] Ошибка HA WebSocket: %s", exc)
            if self.connected:
                self.connected = False
            self.ws = None
            await self.broadcast({"type": "status", "ws": False})
            await asyncio.sleep(5)

    async def _ws_connect(self):
        from websockets.asyncio.client import connect as ws_connect

        url = self.base + "/api/websocket"
        self.ws = await ws_connect(
            url,
            additional_headers={"Authorization": "Bearer " + (self.cfg.get("token") or "")},
            max_size=64 * 1024 * 1024,
            ping_interval=30, ping_timeout=90)
        first = json.loads(await self.ws.recv())
        if first.get("type") != "auth_required":
            raise RuntimeError("неожиданное рукопожатие: %s" % first.get("type"))
        await self.ws.send(json.dumps({"type": "auth",
                                       "access_token": self.cfg.get("token") or ""}))
        resp = json.loads(await self.ws.recv())
        if resp.get("type") != "auth_ok":
            raise RuntimeError("auth_failed: %s" % resp.get("message", ""))
        self.connected = True
        logger.info("[ha] Подключено к Home Assistant WS: %s (api %s)",
                    url, (resp.get("ha_version") or "?"))
        self.gs_id = self._next_id()
        await self.ws.send(json.dumps({"id": self.gs_id, "type": "get_states"}))
        self.sub_id = self._next_id()
        await self.ws.send(json.dumps({"id": self.sub_id, "type": "subscribe_events",
                                       "event_type": "state_changed"}))
        await self.broadcast({"type": "status", "ws": True})
        async for msg in self.ws:
            try:
                data = json.loads(msg if isinstance(msg, str) else msg.decode("utf-8"))
            except Exception:
                swallow.note('ha_web_panel_module._ws_connect:L301')
                continue
            await self._ha_message(data)
        self.connected = False

    async def _ha_message(self, data: dict):
        t = data.get("type")
        mid = data.get("id")

        # 1) события изменения состояний — обновляем кэш и рассылаем всем
        if t == "event":
            ev = data.get("event") or {}
            if ev.get("event_type") == "state_changed":
                sd = ev.get("data") or {}
                eid = sd.get("entity_id")
                ns = sd.get("new_state")
                if eid:
                    if ns:
                        self.states[eid] = ns
                    else:
                        self.states.pop(eid, None)
                    await self.broadcast({"type": "state", "entity_id": eid, "state": ns})

        # 2) результат get_states — полный снимок состояний
        if t == "result" and mid is not None and mid == self.gs_id:
            res = data.get("result") or []
            if isinstance(res, list):
                self.states = {s["entity_id"]: s for s in res
                               if isinstance(s, dict) and "entity_id" in s}
            await self.broadcast({"type": "hello", "ws": True, "states": self.states})
            return

        # 3) проксирование ответов/событий конкретному браузеру
        if mid is not None and mid in self.pending:
            p = self.pending[mid]
            try:
                await p["ws"].send_json(
                    {"type": "proxy_result", "pid": p["pid"], "payload": data})
            except Exception:
                swallow.note('ha_web_panel_module._ha_message:L340')
                self.pending.pop(mid, None)
                return
            if t == "result" and not p["sub"]:
                self.pending.pop(mid, None)

    # ─────────────────────── рассылка браузерам ─────────────────────────
    async def broadcast(self, obj: dict):
        for c in list(self.clients):
            try:
                await c.send_json(obj)
            except Exception:
                swallow.note('ha_web_panel_module.broadcast:L352')
                self.drop_client(c)

    def drop_client(self, client):
        self.clients.discard(client)
        for mid in [k for k, v in self.pending.items() if v["ws"] is client]:
            self.pending.pop(mid, None)

    async def proxy(self, client, msg: dict):
        """Браузер → HA WebSocket (прозрачный прокси команд)."""
        pid = msg.get("pid")
        payload = msg.get("payload") or {}
        if not isinstance(payload, dict) or "type" not in payload:
            return
        if not self.connected or self.ws is None:
            await client.send_json({
                "type": "proxy_result", "pid": pid,
                "payload": {"type": "result", "success": False,
                            "error": {"code": "not_connected",
                                      "message": "Нет WebSocket-соединения с Home Assistant"}}})
            return
        mid = self._next_id()
        ptype = str(payload.get("type", ""))
        sub = ptype.startswith("subscribe_") or ptype == "render_template"
        self.pending[mid] = {"ws": client, "pid": pid, "sub": sub}
        payload = dict(payload)
        payload["id"] = mid
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as exc:
            self.pending.pop(mid, None)
            logger.warning("[ha] proxy send error: %s", exc)

    async def unproxy(self, client, pid):
        for mid in [k for k, v in self.pending.items()
                    if v["ws"] is client and v["pid"] == pid]:
            self.pending.pop(mid, None)
            if self.connected and self.ws is not None:
                try:
                    await self.ws.send(json.dumps({"id": mid,
                                                   "type": "unsubscribe_events"}))
                except Exception:
                    swallow.note('ha_web_panel_module.unproxy:L394')
                    pass

    # ─────────────── резервный REST-опрос (когда WS не жив) ─────────────
    async def _poll_loop(self):
        while True:
            try:
                interval = max(2, int(self.cfg.get("poll_interval") or 5))
                await asyncio.sleep(interval)
                if self.connected or not self.base or not self.cfg.get("token"):
                    continue
                if not self.ensure_session():
                    continue
                async with self.session.get(self.base + "/api/states",
                                             headers=self.headers) as r:
                    if r.status_code != 200:
                        continue
                    new = r.json()
                seen, changed = {}, []
                for s in new:
                    eid = s.get("entity_id")
                    if not eid:
                        continue
                    seen[eid] = s
                    old = self.states.get(eid)
                    if (old is None or old.get("state") != s.get("state")
                            or old.get("last_changed") != s.get("last_changed")):
                        changed.append(s)
                removed = [e for e in self.states if e not in seen]
                self.states = seen
                for s in changed:
                    await self.broadcast(
                        {"type": "state", "entity_id": s["entity_id"], "state": s})
                for e in removed:
                    await self.broadcast({"type": "state", "entity_id": e, "state": None})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[ha] poll error: %s", exc)


_BRIDGES = {}
_BRIDGES_LOCK = threading.RLock()


def get_bridge(profile_id: str = "") -> Bridge:
    """Мост конкретного профиля: у каждого свой HA, токен и кэш состояний."""
    pid = str(profile_id or "").strip() or _default_profile_id()
    with _BRIDGES_LOCK:
        bridge = _BRIDGES.get(pid)
        if bridge is None:
            bridge = Bridge(config_path(pid))
            _BRIDGES[pid] = bridge
        return bridge


def profile_of(request) -> str:
    """Профиль запроса: cookie -> токен агента -> общедоступный."""
    try:
        import profiles_module as PM
        return PM.request_profile(request) or _default_profile_id()
    except Exception:
        swallow.note('ha_web_panel_module.profile_of:L456')
        return _default_profile_id()


def _profile_from_cookies(cookies) -> str:
    try:
        import profiles_module as PM
        return PM.resolve_profile_id(cookies.get(PM.COOKIE_NAME) or "") or _default_profile_id()
    except Exception:
        swallow.note('ha_web_panel_module._profile_from_cookies:L465')
        return _default_profile_id()


async def _start_bridge(bridge: Bridge):
    if bridge.tasks:
        return
    await bridge.start()


async def start():
    """Поднять мосты всех профилей, у которых заданы адрес и токен."""
    pids = {_default_profile_id()}
    try:
        import profiles_module as PM
        pids.update(row.get("id") for row in PM.list_profiles() if row.get("id"))
    except Exception:
        swallow.note('ha_web_panel_module.start:L482')
        pass
    for pid in pids:
        bridge = get_bridge(pid)
        if bridge.base and bridge.cfg.get("token"):
            await _start_bridge(bridge)


async def stop():
    for bridge in list(_BRIDGES.values()):
        await bridge.stop()
    _BRIDGES.clear()


# ═══════════════════════════════ авторизация ══════════════════════════════

def _password(profile_id: str = "") -> str:
    return (get_bridge(profile_id).cfg.get("app_password") or "").strip()


def _check_cookie(cookies, profile_id: str = "") -> bool:
    pw = _password(profile_id)
    if not pw:
        return True
    try:
        return cookies.get("hap_sid") == make_sid(pw, profile_id)
    except Exception:
        swallow.note('ha_web_panel_module._check_cookie:L509')
        return False


def _authorized(request: Request) -> bool:
    return _check_cookie(request.cookies, profile_of(request))


# ═══════════════════════════════ страницы ══════════════════════════════════

def _templates_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _read_page(name: str) -> str:
    path = os.path.join(_templates_dir(), name)
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def _page_html(name: str) -> str:
    """Шаблон панели + единая тема системы (design.css + theme.js)."""
    html = _read_page(name)
    html = html.replace("{{ha_base}}", BASE_PATH)
    try:
        from webui_assets import inject as _inject_ui, add_page_class as _add_pg
        html = _add_pg(html, name)
        html = _inject_ui(html)
    except Exception:
        swallow.note('ha_web_panel_module._page_html:L538')
        pass
    return html


@router.get("", include_in_schema=False)
def ha_index(request: Request):
    """Главная страница панели Home Assistant."""
    if not _authorized(request):
        return RedirectResponse(BASE_PATH + "/login", status_code=302)
    return HTMLResponse(_page_html(PAGE_NAME))


@router.get("/", include_in_schema=False)
def ha_index_slash():
    return RedirectResponse(BASE_PATH, status_code=307)


@router.get("/login", include_in_schema=False)
def ha_login_page():
    return HTMLResponse(_page_html(LOGIN_PAGE_NAME))


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#5eb1ff"/><stop offset="1" stop-color="#7c6cff"/>'
    '</linearGradient></defs>'
    '<rect width="100" height="100" rx="24" fill="url(#g)"/>'
    '<path d="M50 22 78 46h-7v30H57V58H43v18H29V46h-7z" fill="#fff"/></svg>')


@router.get("/favicon.ico", include_in_schema=False)
def ha_favicon():
    return Response(content=FAVICON, media_type="image/svg+xml")


# ═══════════════════════════════ REST API ══════════════════════════════════

@router.post("/api/login")
async def ha_login(request: Request):
    pid = profile_of(request)
    b = get_bridge(pid)
    try:
        data = await request.json()
    except Exception:
        swallow.note('ha_web_panel_module.ha_login:L584')
        data = {}
    password = str(b.cfg.get("app_password") or "")
    if data.get("password") == password:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("hap_sid", make_sid(password, pid),
                        max_age=30 * 86400, httponly=True, samesite="Lax")
        return resp
    return JSONResponse({"error": "Неверный пароль"}, status_code=401)


@router.post("/api/logout")
def ha_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("hap_sid")
    return resp


@router.get("/api/status")
def ha_status(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = get_bridge(profile_of(request))
    return JSONResponse({
        "version": VERSION,
        "configured": bool(b.base and b.cfg.get("token")),
        "ws": b.connected,
        "ha_url": b.base,
        "has_token": bool(b.cfg.get("token")),
        "has_password": bool(b.cfg.get("app_password")),
        "entities": len(b.states),
        "clients": len(b.clients),
        "uptime": int(time.time() - b.started),
        "poll_interval": b.cfg.get("poll_interval", 5),
    })


@router.post("/api/setup")
async def ha_setup(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = get_bridge(profile_of(request))
    try:
        data = await request.json()
    except Exception:
        swallow.note('ha_web_panel_module.ha_setup:L629')
        data = {}
    url = (data.get("ha_url") or "").strip().rstrip("/")
    token = (data.get("token") or "").strip()
    changed = False
    info = None
    if url or token:
        url = url or b.base
        token = token or b.cfg.get("token") or ""
        if not url or not token:
            return JSONResponse({"ok": False, "error": "Укажите адрес HA и токен"},
                                status_code=400)
        if not b.ensure_session():
            return JSONResponse({"ok": False, "error": "httpx недоступен"},
                                status_code=500)
        try:
            r = await b.session.get(url + "/api/",
                                     headers={"Authorization": "Bearer " + token},
                                     timeout=httpx.Timeout(10.0))
            status = r.status_code
            if status == 200:
                try:
                    r2 = await b.session.get(url + "/api/config",
                                              headers={"Authorization": "Bearer " + token},
                                              timeout=httpx.Timeout(10.0))
                    if r2.status_code == 200:
                        ci = r2.json()
                        info = {"version": ci.get("version"),
                                "location": ci.get("location_name")}
                except Exception:
                    swallow.note('ha_web_panel_module.ha_setup:L659')
                    pass
        except Exception as exc:
            return JSONResponse({"ok": False,
                                 "error": "Не удалось соединиться с HA: %s" % exc},
                                status_code=400)
        if status != 200:
            return JSONResponse(
                {"ok": False,
                 "error": "HA ответил кодом %s. Проверьте URL и токен "
                          "(Профиль → Долгосрочные токены доступа)." % status},
                status_code=400)
        if data.get("test"):
            return JSONResponse({"ok": True, "test": True, "info": info})
        changed = (url != b.base) or (token != b.cfg.get("token"))
        b.cfg["ha_url"] = url
        b.cfg["token"] = token
    if "app_password" in data:
        b.cfg["app_password"] = str(data.get("app_password") or "")
    if "poll_interval" in data:
        try:
            b.cfg["poll_interval"] = max(2, min(300, int(data["poll_interval"])))
        except Exception:
            swallow.note('ha_web_panel_module.ha_setup:L682')
            pass
    b.save_cfg()
    if changed:
        b.states = {}
        await b.restart_ws()
    if b.base and b.cfg.get("token"):
        await _start_bridge(b)
    return JSONResponse({"ok": True, "info": info})


@router.get("/api/cam/stream/{entity:path}")
async def ha_cam_stream(request: Request):
    """MJPEG-поток камеры (пасстру потока HA)."""
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = get_bridge(profile_of(request))
    eid = request.path_params.get("entity", "")
    if not b.base or not b.cfg.get("token"):
        return JSONResponse({"error": "Панель не настроена"}, status_code=400)
    if not b.ensure_session():
        return JSONResponse({"error": "httpx недоступен"}, status_code=500)
    url = "%s/api/camera_proxy_stream/%s" % (b.base, eid)
    try:
        req = b.stream_session.build_request("GET", url, headers=b.headers)
        up = await b.stream_session.send(req, stream=True)
    except Exception as exc:
        return JSONResponse({"error": "Ошибка потока: %s" % exc}, status_code=502)

    rct = up.headers.get("content-type") or "multipart/x-mixed-replace;boundary=frame"

    async def _iter():
        try:
            async for chunk in up.aiter_bytes():
                yield chunk
        except Exception:
            swallow.note('ha_web_panel_module._iter:L718')
            pass
        finally:
            try:
                await up.aclose()
            except Exception:
                swallow.note('ha_web_panel_module._iter:L724')
                pass

    return StreamingResponse(_iter(), status_code=up.status_code, media_type=rct,
                             headers={"Cache-Control": "no-store"})


@router.api_route("/api/ha/{tail:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def ha_passthru(request: Request):
    """ANY /ha/api/ha/** → {ha_url}/api/**  (прозрачный REST-прокси с токеном)."""
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = get_bridge(profile_of(request))
    if not b.base or not b.cfg.get("token"):
        return JSONResponse(
            {"error": "Панель не настроена: откройте «Настройки» и укажите адрес HA и токен"},
            status_code=400)
    if not b.ensure_session():
        return JSONResponse({"error": "httpx недоступен"}, status_code=500)
    tail = request.path_params.get("tail", "")
    url = "%s/api/%s" % (b.base, tail)
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = dict(b.headers)
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    try:
        req = b.session.build_request(request.method, url,
                                      content=body or None, headers=headers)
        up = await b.session.send(req, stream=True)
    except Exception as exc:
        return JSONResponse({"error": "Ошибка соединения с HA: %s" % exc},
                            status_code=502)

    rct = up.headers.get("content-type", "application/json")
    if "multipart" in rct or "octet-stream" in rct:
        async def _iter():
            try:
                async for chunk in up.aiter_bytes():
                    yield chunk
            finally:
                try:
                    await up.aclose()
                except Exception:
                    swallow.note('ha_web_panel_module._iter:L771')
                    pass
        return StreamingResponse(_iter(), status_code=up.status_code, media_type=rct,
                                 headers={"Cache-Control": "no-store"})
    try:
        data = await up.aread()
    finally:
        try:
            await up.aclose()
        except Exception:
            swallow.note('ha_web_panel_module.ha_passthru:L781')
            pass
    return Response(content=data, status_code=up.status_code, media_type=rct)


# ═══════════════════════════════ WebSocket ═════════════════════════════════

@router.websocket("/ws")
async def ha_ws(websocket: WebSocket):
    """WebSocket для браузеров: состояния в реальном времени + прокси команд HA."""
    pid = _profile_from_cookies(websocket.cookies)
    b = get_bridge(pid)
    await websocket.accept()
    if not _check_cookie(websocket.cookies, pid):
        try:
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=1008)
        except Exception:
            swallow.note('ha_web_panel_module.ha_ws:L799')
            pass
        return
    if b.base and b.cfg.get("token"):
        await _start_bridge(b)
    client = _Client(websocket)
    b.clients.add(client)
    try:
        await client.send_json({"type": "hello", "ws": b.connected,
                                "states": b.states, "version": VERSION})
        while True:
            raw = await websocket.receive_text()
            try:
                d = json.loads(raw)
            except Exception:
                swallow.note('ha_web_panel_module.ha_ws:L814')
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "proxy":
                await b.proxy(client, d)
            elif t == "unproxy":
                await b.unproxy(client, d.get("pid"))
            elif t == "ping":
                await client.send_json({"type": "pong"})
    except WebSocketDisconnect:
        swallow.note('ha_web_panel_module.ha_ws:L826')
        pass
    except Exception as exc:
        logger.debug("[ha] ws error: %s", exc)
    finally:
        b.drop_client(client)


# ═════════════════════════════ автономный запуск ═══════════════════════════

def main():
    """Запуск панели отдельно от системы (для отладки): python ha_web_panel_module.py"""
    import argparse

    import uvicorn
    from fastapi import FastAPI

    ap = argparse.ArgumentParser(description="HA Web Panel — автономный режим")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8124)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    app = FastAPI(title="HA Web Panel")
    app.include_router(router)

    @app.on_event("startup")
    async def _startup():
        await start()

    @app.on_event("shutdown")
    async def _shutdown():
        await stop()

    print("=" * 68)
    print("  HA WEB PANEL v%s" % VERSION)
    print("  Панель:  http://%s:%d%s" % (args.host, args.port, BASE_PATH))
    print("=" * 68)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
