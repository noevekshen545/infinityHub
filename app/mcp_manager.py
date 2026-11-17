"""
ASIO Agent MCP Manager
Подключение внешних MCP-серверов (stdio/HTTP/SSE) и регистрация их инструментов агенту.

Конфигурация: ~/.infinity/asio_agent_v4/mcp_servers.json
{
  "servers": [
    {"name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
    {"name": "http_server", "url": "http://localhost:8000/mcp"}
  ]
}
Инструменты MCP доступны агенту как [TOOL:mcp_<server>_<tool>] ...
"""

import os
import json
import logging
import queue
import threading
import subprocess
from typing import Dict, List, Any, Optional
import swallow

logger = logging.getLogger("infinity")

AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
os.makedirs(AGENT_BASE, exist_ok=True)
MCP_CONFIG_FILE = os.path.join(AGENT_BASE, "mcp_servers.json")

_lock = threading.Lock()
_clients: Dict[str, Any] = {}
_tools_cache: Dict[str, Dict[str, Any]] = {}


def _load_config() -> Dict:
    if os.path.exists(MCP_CONFIG_FILE):
        try:
            with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[MCP] Config load error: {e}")
    return {"servers": []}


def _save_config(cfg: Dict):
    try:
        with open(MCP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[MCP] Config save error: {e}")


class _StdioClient:
    """MCP клиент через stdio subprocess (локальные серверы)."""

    def __init__(self, name: str, command: str, args: List[str],
                 env: Optional[Dict[str, str]] = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = dict(env or {})
        self.proc = None
        self._lock = threading.Lock()
        self._id = 0
        self._pending: Dict[int, "queue.Queue"] = {}
        self._read_thread = None

    def start(self) -> bool:
        try:
            child_env = None
            if self.env:
                child_env = {**os.environ, **{str(k): str(v) for k, v in self.env.items()}}
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=child_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            import queue
            self._pending = {}
            self._read_thread = threading.Thread(target=self._reader, daemon=True)
            self._read_thread.start()
            return True
        except Exception as e:
            logger.error(f"[MCP] stdio start error: {e}")
            return False

    def _reader(self):
        import queue
        while self.proc and self.proc.poll() is None:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
                rid = msg.get("id")
                if rid in self._pending:
                    self._pending.pop(rid).put(msg)
            except Exception:
                swallow.note('mcp_manager._reader:L99')
                pass

    def request(self, method: str, params: Dict = None) -> Optional[Dict]:
        import queue
        if not self.proc or self.proc.poll() is not None:
            return None
        self._id += 1
        req_id = self._id
        q = queue.Queue()
        self._pending[req_id] = q
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            msg = q.get(timeout=30)
            if "result" in msg:
                return msg["result"]
            if "error" in msg:
                logger.error(f"[MCP] {self.name} error: {msg['error']}")
        except Exception as e:
            logger.error(f"[MCP] {self.name} request error: {e}")
        return None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                swallow.note('mcp_manager.stop:L129')
                pass


class _HttpClient:
    """MCP клиент через HTTP (streamable HTTP transport)."""

    def __init__(self, name: str, url: str,
                 headers: Optional[Dict[str, str]] = None):
        self.name = name
        self.url = url
        self.headers = dict(headers or {})
        self.session_id = None

    def request(self, method: str, params: Dict = None) -> Optional[Dict]:
        import urllib.request
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update({str(k): str(v) for k, v in self.headers.items()})
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.session_id = resp.headers.get("Mcp-Session-Id") or self.session_id
                ctype = resp.headers.get("Content-Type", "")
                body = resp.read().decode("utf-8", errors="replace")
                if "text/event-stream" in ctype:
                    for line in body.splitlines():
                        if line.startswith("data:"):
                            msg = json.loads(line[5:].strip())
                            if "result" in msg:
                                return msg["result"]
                            if "error" in msg:
                                logger.error(f"[MCP] {self.name} error: {msg['error']}")
                                return None
                    return None
                msg = json.loads(body)
                return msg.get("result")
        except Exception as e:
            logger.error(f"[MCP] {self.name} http error: {e}")
        return None


def _get_client(server: Dict) -> Optional[Any]:
    name = server.get("name", "")
    with _lock:
        if name in _clients:
            return _clients[name]
        if server.get("command"):
            client = _StdioClient(name, server["command"], server.get("args", []),
                                  env=server.get("env"))
        elif server.get("url"):
            client = _HttpClient(name, server["url"],
                                 headers=server.get("headers"))
        else:
            logger.error(f"[MCP] Server '{name}' has no command/url")
            return None
        if client.start():
            _clients[name] = client
            return client
        return None


def list_servers() -> Dict:
    cfg = _load_config()
    servers = []
    for s in cfg.get("servers", []):
        servers.append({"name": s.get("name"), "type": "stdio" if s.get("command") else "http", "url": s.get("url", ""), "command": s.get("command", ""), "connected": s.get("name") in _clients})
    return {"servers": servers}


def add_server(name: str, command: str = "", args: List[str] = None, url: str = "",
               headers: Dict[str, str] = None, env: Dict[str, str] = None) -> Dict:
    cfg = _load_config()
    for s in cfg.get("servers", []):
        if s.get("name") == name:
            return {"ok": False, "error": f"Сервер '{name}' уже существует"}
    row = {"name": name, "command": command, "args": args or [], "url": url}
    if headers:
        row["headers"] = {str(k): str(v) for k, v in headers.items()}
    if env:
        row["env"] = {str(k): str(v) for k, v in env.items()}
    cfg.setdefault("servers", []).append(row)
    _save_config(cfg)
    return {"ok": True}


def remove_server(name: str) -> Dict:
    cfg = _load_config()
    cfg["servers"] = [s for s in cfg.get("servers", []) if s.get("name") != name]
    _save_config(cfg)
    with _lock:
        if name in _clients:
            try:
                _clients[name].stop()
            except Exception:
                swallow.note('mcp_manager.remove_server:L226')
                pass
            del _clients[name]
    return {"ok": True}


def list_tools(server_name: str = None) -> Dict:
    """Получает список инструментов от подключённых MCP-серверов."""
    cfg = _load_config()
    result = {"tools": [], "servers": 0}
    for s in cfg.get("servers", []):
        name = s.get("name", "")
        if server_name and name != server_name:
            continue
        client = _get_client(s)
        if not client:
            continue
        resp = client.request("tools/list")
        if resp and resp.get("tools"):
            result["servers"] += 1
            for t in resp["tools"]:
                full = f"mcp_{name}_{t.get('name', '')}"
                result["tools"].append({
                    "name": full,
                    "server": name,
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {})
                })
                _tools_cache[full] = {"server": name, "original": t.get("name", ""), "inputSchema": t.get("inputSchema", {})}
    return result


def call_tool(tool_full_name: str, args: str) -> Dict:
    """Вызывает MCP-инструмент. tool_full_name: mcp_<server>_<tool>"""
    info = _tools_cache.get(tool_full_name)
    if not info:
        list_tools()
        info = _tools_cache.get(tool_full_name)
    if not info:
        return {"success": False, "output": f"❌ MCP-инструмент не найден: {tool_full_name}. Сначала вызови mcp_list_tools", "tool": tool_full_name}
    server_name = info["server"]
    cfg = _load_config()
    server_cfg = next((s for s in cfg.get("servers", []) if s.get("name") == server_name), None)
    if not server_cfg:
        return {"success": False, "output": f"❌ MCP-сервер не найден: {server_name}", "tool": tool_full_name}
    client = _get_client(server_cfg)
    if not client:
        return {"success": False, "output": f"❌ Не удалось подключиться к MCP-серверу: {server_name}", "tool": tool_full_name}

    import ast
    params = {}
    try:
        parsed = ast.literal_eval(args) if args.strip().startswith(("{", "[")) else None
        if isinstance(parsed, dict):
            params = parsed
        else:
            params = _parse_args_text(args, info.get("inputSchema", {}))
    except Exception:
        # штатный переход на текстовый парсер, но след ошибки должен остаться
        swallow.note('mcp_manager.call_tool:L284')
        params = _parse_args_text(args, info.get("inputSchema", {}))

    resp = client.request("tools/call", {"name": info["original"], "arguments": params})
    if resp is None:
        return {"success": False, "output": f"❌ MCP-вызов не вернул результат ({server_name}:{info['original']})", "tool": tool_full_name}
    content = resp.get("content", [])
    text_parts = []
    for c in content:
        if c.get("type") == "text":
            text_parts.append(c.get("text", ""))
        elif c.get("type") == "image":
            text_parts.append("[изображение]")
    is_error = resp.get("isError", False)
    output = "\n".join(text_parts) or json.dumps(resp, ensure_ascii=False)[:2000]
    return {"success": not is_error, "output": output, "tool": tool_full_name}


def _parse_args_text(args: str, schema: Dict) -> Dict:
    """Пытается распарсить аргументы из текстового формата key: value | key2: value2"""
    params = {}
    properties = schema.get("properties", {})
    for part in args.split("|"):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if k in properties:
                ptype = properties[k].get("type", "string")
                if ptype == "number":
                    try:
                        v = float(v)
                    except ValueError:
                        swallow.note('mcp_manager._parse_args_text:L319')
                        pass
                elif ptype == "integer":
                    try:
                        v = int(v)
                    except ValueError:
                        swallow.note('mcp_manager._parse_args_text:L325')
                        pass
                elif ptype == "boolean":
                    v = v.lower() in ("true", "1", "да")
                elif ptype == "array":
                    try:
                        import ast
                        v = ast.literal_eval(v)
                    except Exception:
                        swallow.note('mcp_manager._parse_args_text:L334')
                        v = [x.strip() for x in v.split(",")]
                params[k] = v
    return params


def tool_mcp_list_servers(args=""):
    try:
        data = list_servers()
        if not data["servers"]:
            return {"success": True, "output": "🔌 MCP-серверы не настроены. Добавь через [TOOL:mcp_add_server]", "servers": [], "tool": "mcp_list_servers"}
        out = "🔌 MCP-серверы:\n\n" + "\n".join(f"• **{s['name']}** ({s['type']}) {'🟢 подключён' if s['connected'] else '⚪ не подключён'}" for s in data["servers"])
        return {"success": True, "output": out, "servers": data["servers"], "tool": "mcp_list_servers"}
    except Exception as e:
        swallow.note('mcp_manager.tool_mcp_list_servers:L348', e)
        return {"success": False, "output": f"❌ Ошибка: {e}", "tool": "mcp_list_servers"}


def tool_mcp_list_tools(args=""):
    try:
        data = list_tools()
        if not data["tools"]:
            return {"success": True, "output": "🔌 MCP-инструменты не найдены. Проверь серверы: [TOOL:mcp_list_servers]", "tools": [], "tool": "mcp_list_tools"}
        out = f"🔌 MCP-инструменты ({len(data['tools'])}):\n\n"
        for t in data["tools"]:
            out += f"• **{t['name']}** ({t['server']})\n  {t['description'][:150]}\n\n"
        return {"success": True, "output": out, "tools": data["tools"], "tool": "mcp_list_tools"}
    except Exception as e:
        swallow.note('mcp_manager.tool_mcp_list_tools:L362', e)
        return {"success": False, "output": f"❌ Ошибка: {e}", "tool": "mcp_list_tools"}


def tool_mcp_add_server(args=""):
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 2:
        return {"success": False, "output": "❌ Формат: name | command или name | url", "tool": "mcp_add_server"}
    name = parts[0]
    if not name or name in ("name", "none"):
        return {"success": False, "output": "❌ Неверное имя сервера", "tool": "mcp_add_server"}
    if parts[1].startswith("http"):
        res = add_server(name, url=parts[1])
    else:
        res = add_server(name, command=parts[1], args=parts[2:])
    if res.get("ok"):
        return {"success": True, "output": f"✅ MCP-сервер '{name}' добавлен", "tool": "mcp_add_server"}
    return {"success": False, "output": res.get("error", "Ошибка"), "tool": "mcp_add_server"}


def tool_mcp_remove_server(args=""):
    name = args.strip()
    if not name:
        return {"success": False, "output": "❌ Формат: name", "tool": "mcp_remove_server"}
    remove_server(name)
    return {"success": True, "output": f"✅ MCP-сервер '{name}' удалён", "tool": "mcp_remove_server"}


def tool_mcp_call(args=""):
    """Вызывает MCP-инструмент: mcp_<server>_<tool> | args"""
    from agent_tools import _split_pipe_args
    tool_name, tool_args = _split_pipe_args(args)
    return call_tool(tool_name, tool_args)