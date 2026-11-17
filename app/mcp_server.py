"""MCP-сервер Infinity: каталог действий как инструменты для внешних агентов.

Транспорт — HTTP Streamable MCP (JSON-RPC 2.0) в routes/mcp.py, плюс
stdio-обёртка mcp_stdio.py для клиентов без HTTP. Доступ выключен, пока не
включён модуль mcp_server.enabled; запись — отдельный флаг write.

Авторизация — долгоживущий ключ профиля (inf_mcp_..., в файле только sha256),
отзывается в AI-центре. Для loopback без ключа профиль берётся из активного
только при отдельном флаге loopback_no_key.

Инструменты — типизированные действия action_center (проверка параметров,
read_only) и обобщённый app_api: method+path по внутренним /api с блок-листом
(сам MCP, AI-центр, каталог действий, профили, админка, recovery, auth).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import swallow

logger = logging.getLogger("infinity")

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "infinity"
SERVER_VERSION = "1.0"
_MAX_BODY = 1024 * 1024
_MAX_RESPONSE = 2 * 1024 * 1024
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Пути, которые обобщённый app_api не трогает ни при каких правах: управление
# ключами/профилями/админкой и сам каталог — иначе ключ профиля мог бы выдать
# себе больше прав или обойти проверку параметров.
_API_BLOCKED = (
    "/api/mcp",
    "/api/actions",
    "/api/ai-center",
    "/api/admin",
    "/api/profiles",
    "/api/recovery",
    "/api/auth",
)
_API_PATH_RE = re.compile(r"^/api/[A-Za-z0-9_./-]*$")
# Точечные маршруты, которые пишут флаги модулей (включая mcp_server.write):
# через них агент мог бы включить себе запись в обход AI-центра.
_API_BLOCKED_EXACT = ("/api/modules/flag",)
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class McpAuthError(Exception):
    pass


# ── флаги ───────────────────────────────────────────────────────────────────

def _flag(key: str) -> bool:
    try:
        import module_flags
        return module_flags.get_flag("mcp_server", key)
    except Exception:
        swallow.note('mcp_server._flag:L75')
        return False


def enabled() -> bool:
    return _flag("enabled")


def writes_allowed() -> bool:
    return enabled() and _flag("write")


def loopback_no_key() -> bool:
    return enabled() and _flag("loopback_no_key")


def port() -> int:
    """Порт живого сервера: --port/INFINITY_PORT важнее профильного конфига.

    Рабочий стол запускает сервер с `--port 8281`, а профильный config.json
    исторически хранит 8181: по CFG адрес для app_api промахивался бы мимо
    слушателя.
    """
    try:
        import server
    except Exception:
        swallow.note('mcp_server.port:L101')
        return 8080
    for value in (getattr(server, "HTTP_PORT", None),
                  os.environ.get("INFINITY_PORT"),
                  (getattr(server, "CFG", {}) or {}).get("server", {}).get("port")):
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            swallow.note('mcp_server.port:L110')
            continue
    return 8080


# ── ключи профилей ──────────────────────────────────────────────────────────

_KEYS_LOCK = threading.RLock()
_KEYS_CACHE = {"mtime": None, "data": None}


def _keys_path() -> str:
    import profiles_module as PM
    return os.path.join(PM.DATA_DIR, "mcp_keys.json")


def _blank_keys() -> Dict[str, Any]:
    return {"version": 1, "keys": []}


def _load_keys() -> Dict[str, Any]:
    path = _keys_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        swallow.note('mcp_server._load_keys:L135')
        mtime = None
    with _KEYS_LOCK:
        if _KEYS_CACHE["data"] is not None and _KEYS_CACHE["mtime"] == mtime:
            return _KEYS_CACHE["data"]
    data = _blank_keys()
    if mtime is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            rows = raw.get("keys") if isinstance(raw, dict) else None
            if isinstance(rows, list):
                data["keys"] = [r for r in rows if isinstance(r, dict) and r.get("hash")]
        except Exception as e:
            logger.warning(f"[mcp] ключи не прочитаны: {e}")
    with _KEYS_LOCK:
        _KEYS_CACHE["mtime"] = mtime
        _KEYS_CACHE["data"] = data
    return data


def _save_keys(data: Dict[str, Any]) -> None:
    path = _keys_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    with _KEYS_LOCK:
        _KEYS_CACHE["data"] = data
        try:
            _KEYS_CACHE["mtime"] = os.path.getmtime(path)
        except OSError:
            swallow.note('mcp_server._save_keys:L168')
            _KEYS_CACHE["mtime"] = None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name") or "MCP",
        "profile": row.get("profile"),
        "created": int(row.get("created") or 0),
        "last_used": int(row.get("last_used") or 0),
    }


def list_keys() -> List[Dict[str, Any]]:
    rows = sorted(_load_keys()["keys"], key=lambda r: r.get("created") or 0,
                  reverse=True)
    return [_public(r) for r in rows]


def issue_key(name: str = "", profile_id: str = "") -> Dict[str, Any]:
    """Создать ключ профиля. Полный ключ возвращается один раз и не хранится."""
    import profiles_module as PM
    pid = str(profile_id or PM.get_active_id() or "").strip()
    if not pid or not PM.profile_exists(pid):
        raise ValueError(f"Профиль не найден: {pid or '(пусто)'}")
    key = "inf_mcp_" + secrets.token_urlsafe(24)
    row = {
        "id": "mk_" + secrets.token_hex(6),
        "name": (str(name or "").strip() or "MCP")[:60],
        "profile": pid,
        "hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "created": int(time.time()),
        "last_used": 0,
    }
    data = _load_keys()
    data["keys"].append(row)
    _save_keys(data)
    out = _public(row)
    out["key"] = key
    return out


def revoke_key(key_id: str) -> str:
    kid = str(key_id or "").strip()
    data = _load_keys()
    before = len(data["keys"])
    data["keys"] = [r for r in data["keys"] if r.get("id") != kid]
    if len(data["keys"]) == before:
        raise ValueError(f"Ключ не найден: {kid}")
    _save_keys(data)
    return kid


def resolve_key(token: str) -> Optional[str]:
    """Профиль по ключу; отмечает last_used не чаще раза в минуту."""
    token = (token or "").strip()
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    data = _load_keys()
    hit = None
    for row in data["keys"]:
        if hmac.compare_digest(str(row.get("hash") or ""), digest):
            hit = row
            break
    if not hit:
        return None
    import profiles_module as PM
    pid = str(hit.get("profile") or "")
    if not PM.profile_exists(pid) or PM._is_blocked_profile(pid):
        return None
    now = int(time.time())
    if now - int(hit.get("last_used") or 0) > 60:
        hit["last_used"] = now
        try:
            _save_keys(data)
        except Exception as e:
            logger.warning(f"[mcp] last_used не сохранён: {e}")
    return pid


def authenticate(request) -> str:
    """Профиль вызывающего по ключу; loopback без ключа — только под флагом."""
    token = ""
    try:
        auth = request.headers.get("authorization", "") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.headers.get("x-infinity-mcp-key", "") or ""
    except Exception:
        swallow.note('mcp_server.authenticate:L260')
        token = ""
    if token:
        pid = resolve_key(token)
        if pid:
            return pid
        raise McpAuthError("Ключ MCP недействителен или отозван")
    if loopback_no_key():
        import profiles_module as PM
        if PM._is_loopback_request(request):
            return PM.get_active_id()
    raise McpAuthError("Нужен ключ MCP (Authorization: Bearer inf_mcp_...)")


# ── инструменты ─────────────────────────────────────────────────────────────

def tool_name(action_id: str) -> str:
    name = _TOOL_NAME_RE.sub("_", str(action_id or "")).strip("_")
    return name[:64] or "action"


def _json_schema(params_spec: Dict[str, Any]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    required: List[str] = []
    for name, ps in (params_spec or {}).items():
        kind = str(ps.get("type") or "string")
        if kind == "int":
            node: Dict[str, Any] = {"type": "integer"}
            if ps.get("min") is not None:
                node["minimum"] = ps["min"]
            if ps.get("max") is not None:
                node["maximum"] = ps["max"]
        elif kind == "bool":
            node = {"type": "boolean"}
        elif kind == "list":
            node = {"type": "array", "items": {"type": "string"}}
        elif kind == "enum":
            node = {"type": "string", "enum": list(ps.get("choices") or [])}
        else:
            node = {"type": "string"}
            if kind == "date":
                node["format"] = "date"
            elif kind == "time":
                node["format"] = "time"
        if ps.get("description"):
            node["description"] = ps["description"]
        if ps.get("default") is not None:
            node["default"] = ps["default"]
        props[name] = node
        if ps.get("required"):
            required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _action_tool(spec) -> Dict[str, Any]:
    import ai_center
    parts = [spec.title, spec.description]
    try:
        prompt = ai_center.prompt_for(spec.id)
        if prompt:
            parts.append("Инструкция: " + prompt)
    except Exception:
        swallow.note('mcp_server._action_tool:L325')
        pass
    parts.append(f"Действие каталога: {spec.id} (категория {spec.category}).")
    return {
        "name": tool_name(spec.id),
        "description": " ".join(p for p in parts if p)[:4000],
        "inputSchema": _json_schema(spec.params),
        "annotations": {"readOnlyHint": bool(spec.read_only)},
    }


def _app_api_tool() -> Dict[str, Any]:
    methods = list(_METHODS) if writes_allowed() else ["GET"]
    return {
        "name": "app_api",
        "description": (
            "Обобщённый вызов внутреннего API приложения от имени профиля: "
            "method + path + params. GET передаёт params в query, остальные — "
            "в JSON-тело. Закрыты: /api/mcp, /api/actions, /api/ai-center, "
            "/api/admin, /api/profiles, /api/recovery, /api/auth. "
            "Управление расширениями — только чтение через GET. "
            "Ответ ограничен 2 МБ."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": methods,
                           "description": "HTTP-метод"},
                "path": {"type": "string",
                         "description": "Внутренний путь вида /api/module/…"},
                "params": {"type": "object",
                           "description": "Параметры запроса или JSON-тело"},
            },
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": not writes_allowed()},
    }


def tools_payload() -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    import action_center
    import ai_center
    try:
        ai_center.ensure_synced()
    except Exception:
        swallow.note('mcp_server.tools_payload:L370')
        pass
    tools: List[Dict[str, Any]] = []
    mapping: Dict[str, str] = {}
    used = {"app_api"}
    for spec in sorted(action_center.ACTIONS.values(),
                       key=lambda a: (a.category, a.id)):
        if not spec.read_only and not writes_allowed():
            continue
        base = tool_name(spec.id)
        name, i = base, 2
        while name in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name)
        mapping[name] = spec.id
        tools.append(_action_tool(spec))
    tools.append(_app_api_tool())
    return tools, mapping


# ── профиль и выполнение ────────────────────────────────────────────────────

def _ensure_profile(profile_id: str) -> None:
    """Переключить глобалы на профиль ключа (как это делает middleware cookie)."""
    pid = str(profile_id or "").strip()
    if not pid:
        return
    import profiles_module as PM
    try:
        if PM.get_active_id() == pid:
            return
    except Exception:
        swallow.note('mcp_server._ensure_profile:L403')
        return
    # server тянет тяжёлый монолит, поэтому импортируем только при реальном
    # переключении профиля.
    import server
    try:
        locked = server._PROFILE_FLIP_LOCK.acquire(timeout=5.0)
    except Exception:
        swallow.note('mcp_server._ensure_profile:L411')
        locked = False
    if not locked:
        raise McpError(-32603, "Профиль занят переключением, повторите запрос")
    try:
        if PM.switch_profile(pid):
            server.apply_profile_globals(profile_id=pid)
    finally:
        server._PROFILE_FLIP_LOCK.release()


def _api_path_error(method: str, path: str) -> Optional[str]:
    text = str(path or "").strip()
    if not _API_PATH_RE.match(text):
        return "path: только внутренний путь /api/… без query и .."
    if ".." in text or "//" in text or "\\" in text:
        return "path: недопустимые сегменты"
    low = text.rstrip("/")
    if low in _API_BLOCKED_EXACT:
        return f"path: {low} закрыт для app_api"
    for blocked in _API_BLOCKED:
        if low == blocked or low.startswith(blocked + "/"):
            return f"path: {blocked} закрыт для app_api"
    if method != "GET" and (low == "/api/extensions"
                            or low.startswith("/api/extensions/")):
        return "path: управление расширениями доступно только на чтение"
    return None


async def _call_app_api(profile_id: str, args: Dict[str, Any]) -> Any:
    import httpx
    import profiles_module as PM

    method = str(args.get("method") or "GET").strip().upper()
    path = str(args.get("path") or "").strip()
    params = args.get("params")
    if method not in _METHODS:
        raise McpError(-32602, "method: одно из " + ", ".join(_METHODS))
    if method != "GET" and not writes_allowed():
        raise McpError(-32602, "Запись выключена (mcp_server.write=false)")
    if params is not None and not isinstance(params, dict):
        raise McpError(-32602, "params: нужен объект")
    problem = _api_path_error(method, path)
    if problem:
        raise McpError(-32602, problem)
    low = path.rstrip("/")
    if low == "/api/config/save" and isinstance(params, dict):
        cfg = params.get("config")
        mod = ((cfg or {}).get("modules") or {}).get("mcp_server") \
            if isinstance(cfg, dict) else None
        if isinstance(mod, dict):
            for key in ("enabled", "write", "loopback_no_key"):
                if key in mod and bool(mod[key]) != _flag(key):
                    raise McpError(
                        -32602,
                        "Флаги MCP-сервера меняются только в AI-центре")

    await asyncio.get_running_loop().run_in_executor(None, _ensure_profile,
                                                     profile_id)
    url = f"http://127.0.0.1:{port()}{path}"
    headers = {"Accept": "application/json",
               "X-Infinity-Profile-Token": PM.make_agent_token(profile_id)}
    kwargs: Dict[str, Any] = {}
    if method == "GET":
        kwargs["params"] = params or {}
    else:
        kwargs["json"] = params or {}
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
    except Exception as e:
        raise McpError(-32603, f"API недоступен: {e}")
    if len(resp.content) > _MAX_RESPONSE:
        raise McpError(-32603, "Ответ API слишком большой")
    ctype = resp.headers.get("content-type", "")
    body: Any
    if "json" in ctype:
        try:
            body = resp.json()
        except Exception:
            swallow.note('mcp_server._call_app_api:L492')
            body = resp.text[:100000]
    else:
        body = resp.text[:100000]
    if resp.status_code >= 400:
        text = ""
        if isinstance(body, dict):
            text = str(body.get("error") or body.get("detail") or "")
        else:
            text = str(body or "")
        raise McpError(-32603, text[:500] or f"API ответил {resp.status_code}",
                       {"upstream_status": resp.status_code})
    return body


async def call_tool(profile_id: str, name: str, args: Optional[Dict[str, Any]]) -> Any:
    import action_center

    tool = str(name or "").strip()
    arguments = args if isinstance(args, dict) else {}
    if tool == "app_api":
        return await _call_app_api(profile_id, arguments)

    _, mapping = tools_payload()
    action_id = mapping.get(tool)
    if action_id is None and tool in action_center.ACTIONS:
        action_id = tool
    if action_id is None:
        raise McpError(-32602, f"Неизвестный инструмент: {tool}")
    spec = action_center.get(action_id)
    if not spec.read_only and not writes_allowed():
        raise McpError(-32602, "Запись выключена (mcp_server.write=false)")
    await asyncio.get_running_loop().run_in_executor(None, _ensure_profile,
                                                     profile_id)
    return await action_center.run(action_id, arguments, profile_id)


# ── JSON-RPC ────────────────────────────────────────────────────────────────

_INSTRUCTIONS = (
    "Infinity — локальное приложение. Инструменты выполняются от имени профиля "
    "ключа в пределах этого профиля; настройки и данные других профилей "
    "недоступны. app_api вызывает внутренние /api приложения; предпочитай "
    "типизированные инструменты, когда они есть."
)


async def handle_message(msg: Any, profile_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request")
    mid = msg.get("id")
    if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(mid, -32600, "Invalid Request")
    method = msg["method"]
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": _INSTRUCTIONS,
        }
        return _result(mid, result)
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        tools, _ = tools_payload()
        return _result(mid, {"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            return _error(mid, -32602, "tools/call: нужен name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(mid, -32602, "arguments: нужен объект")
        try:
            data = await call_tool(profile_id, name, arguments)
        except McpError as e:
            if e.code == -32602:
                return _error(mid, e.code, e.message, e.data)
            return _tool_error(mid, e.message)
        except Exception as e:
            logger.exception("mcp tool %s failed", name)
            return _tool_error(mid, f"внутренняя ошибка: {e}")
        return _tool_result(mid, data)
    return _error(mid, -32601, f"Method not found: {method}")


def _result(mid: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def _tool_result(mid: Any, data: Any) -> Dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return _result(mid, {"content": [{"type": "text", "text": text}],
                         "isError": False})


def _tool_error(mid: Any, message: str) -> Dict[str, Any]:
    return _result(mid, {"content": [{"type": "text", "text": message}],
                         "isError": True})
