"""stdio-обёртка MCP: мост между stdio-клиентом и HTTP /mcp приложения.

Нужна клиентам, которые умеют только stdio (Claude Desktop и подобные).
Сообщения stdio — JSON-RPC по одному в строке; ответы печатаются так же.
Настройки через окружение:
    INFINITY_MCP_URL=http://127.0.0.1:8281/mcp
    INFINITY_MCP_KEY=inf_mcp_...   (ключ из AI-центра, необязателен для loopback
                                    при включённом mcp_server.loopback_no_key)

Запуск: python app/mcp_stdio.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
import swallow

URL = os.environ.get("INFINITY_MCP_URL", "http://127.0.0.1:8281/mcp").strip()
KEY = os.environ.get("INFINITY_MCP_KEY", "").strip()
TIMEOUT = float(os.environ.get("INFINITY_MCP_TIMEOUT", "60"))


def _post(body: bytes) -> tuple:
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        swallow.note('mcp_stdio._post:L34', e)
        return e.code, e.read()
    except Exception as e:
        return 0, json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32000, "message": f"HTTP недоступен: {e}"},
        }).encode("utf-8")


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    if not URL:
        print("[mcp-stdio] INFINITY_MCP_URL пуст", file=sys.stderr)
        sys.exit(2)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _emit({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
            continue
        status, body = _post(line.encode("utf-8"))
        if not body:
            if status >= 400:
                mid = msg.get("id") if isinstance(msg, dict) else None
                _emit({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32000,
                                 "message": f"HTTP {status}"}})
            continue
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            mid = msg.get("id") if isinstance(msg, dict) else None
            _emit({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32000,
                             "message": f"HTTP {status}: ответ не JSON"}})
            continue
        if isinstance(data, list):
            for item in data:
                _emit(item)
        else:
            # 401/404 приходят с id=null: подставим id запроса, иначе клиент
            # не сопоставит ошибку со своим вызовом.
            if (status >= 400 and isinstance(data, dict)
                    and data.get("id") is None and isinstance(msg, dict)):
                data["id"] = msg.get("id")
            _emit(data)


if __name__ == "__main__":
    main()
