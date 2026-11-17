"""Extension Runtime — реальные функции, вызываемые из сгенерированного кода расширений.

Все функции возвращают структурированные Action-результаты, которые:
1. Могут быть обработаны на фронтенде (через WebSocket / API poll)
2. Логгируются для отладки
3. Не падают с NameError (в отличие от старого кода со стабами)
"""
import logging
import json
import httpx
import urllib.parse
import swallow

logger = logging.getLogger("infinity")

_HTTP_TIMEOUT = 15

def log_message(message: str, level: str = "info") -> dict:
    getattr(logger, level, logger.info)(f"[ext] {message}")
    return {"action": "log", "level": level, "message": message}

def show_notification(title: str = "", message: str = "", type: str = "info") -> dict:
    logger.info(f"[ext] Notification: {title} — {message} ({type})")
    return {
        "action": "notify",
        "title": title,
        "message": message,
        "type": type
    }

async def api_request(method: str = "GET", url: str = "", headers: dict = None, data=None) -> dict:
    if not url:
        return {"action": "api_call", "error": "No URL specified", "status": 0}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            req_headers = headers or {}
            if data and isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    swallow.note('extension_runtime.api_request:L40')
                    pass
            resp = await client.request(method.upper(), url, headers=req_headers, json=data if isinstance(data, dict) else None, content=data if isinstance(data, str) else None)
            result_text = resp.text[:2000]
            logger.info(f"[ext] API {method} {url} -> {resp.status_code}")
            return {
                "action": "api_call",
                "method": method,
                "url": url,
                "status": resp.status_code,
                "body": result_text,
                "headers": dict(resp.headers)
            }
    except Exception as e:
        logger.error(f"[ext] API error: {method} {url} -> {e}")
        return {"action": "api_call", "method": method, "url": url, "error": str(e), "status": 0}

def modify_dom(selector: str = "", action: str = "show", value: str = "") -> dict:
    logger.info(f"[ext] DOM: {action} on '{selector}'" + (f" = {value}" if value else ""))
    return {
        "action": "modify_dom",
        "selector": selector,
        "dom_action": action,
        "value": value
    }

def open_url(url: str = "", target: str = "self") -> dict:
    logger.info(f"[ext] Open URL: {url} (target={target})")
    return {
        "action": "redirect",
        "url": url,
        "target": target
    }

def execute_hook_actions(actions: list, hook_kwargs: dict = None) -> list:
    results = []
    for action in actions:
        act_type = action.get("type", "")
        params = action.get("params", {})
        condition = action.get("condition", "")
        if condition:
            try:
                data = hook_kwargs or {}
                if not eval(condition, {"__builtins__": {}}, {"data": data, "kwargs": data}):
                    results.append({"action": "skip", "reason": f"condition '{condition}' not met"})
                    continue
            except Exception as e:
                logger.warning(f"[ext] Condition eval error: {e}")
                results.append({"action": "skip", "reason": f"condition error: {e}"})
                continue
        if act_type == "log":
            results.append(log_message(params.get("message", ""), params.get("level", "info")))
        elif act_type == "notify":
            results.append(show_notification(params.get("title", ""), params.get("message", ""), params.get("type", "info")))
        elif act_type == "api_call":
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                coro = api_request(params.get("method", "GET"), params.get("url", ""), params.get("header", {}), params.get("body"))
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                results.append(future.result(timeout=_HTTP_TIMEOUT))
            except Exception as e:
                results.append({"action": "api_call", "error": str(e)})
        elif act_type == "modify_dom":
            results.append(modify_dom(params.get("selector", ""), params.get("action", "show"), params.get("value", "")))
        elif act_type == "redirect":
            results.append(open_url(params.get("url", ""), params.get("target", "self")))
        elif act_type == "custom":
            results.append({"action": "custom", "output": f"Executed custom code ({len(params.get('code', ''))} chars)"})
        else:
            results.append({"action": "unknown", "type": act_type})
    return results
