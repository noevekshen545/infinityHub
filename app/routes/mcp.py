"""HTTP-транспорт MCP-сервера (Streamable HTTP, JSON-RPC 2.0).

POST /mcp принимает одиночное сообщение или пакет (старые клиенты), GET
отвечает 405 (SSE-поток сервер не открывает), DELETE завершает сессию.
Модуль выключен по умолчанию: при mcp_server.enabled=false все методы дают 404,
поэтому сама точка не светится. Доступ — ключ профиля (Bearer) или, под
отдельным флагом, loopback с активным профилем.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import mcp_server
import swallow

logger = logging.getLogger("infinity")
router = APIRouter(tags=["mcp"])

_MAX_BATCH = 50


def _hidden() -> JSONResponse:
    return JSONResponse({"error": "MCP-сервер выключен"}, status_code=404)


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None,
         "error": {"code": -32001, "message": message}},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="infinity-mcp"'},
    )


def _rpc_error(mid, code, message, status=400):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": mid,
         "error": {"code": code, "message": message}},
        status_code=status)


@router.post("/mcp")
async def mcp_post(request: Request):
    if not mcp_server.enabled():
        return _hidden()
    try:
        profile_id = mcp_server.authenticate(request)
    except mcp_server.McpAuthError as e:
        swallow.note('mcp.mcp_post:L51', e)
        return _unauthorized(str(e))

    raw = await request.body()
    if len(raw) > mcp_server._MAX_BODY:
        return _rpc_error(None, -32600, "Сообщение больше 1 МБ", 413)
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return _rpc_error(None, -32700, "Parse error")

    if isinstance(payload, list):
        if not payload:
            return _rpc_error(None, -32600, "Invalid Request")
        if len(payload) > _MAX_BATCH:
            return _rpc_error(None, -32600, "Слишком большой пакет", 413)
        responses = []
        for msg in payload:
            resp = await mcp_server.handle_message(msg, profile_id)
            if resp is not None:
                responses.append(resp)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)

    resp = await mcp_server.handle_message(payload, profile_id)
    if resp is None:
        return Response(status_code=202)
    return JSONResponse(resp)


@router.get("/mcp")
async def mcp_get():
    if not mcp_server.enabled():
        return _hidden()
    # SSE-поток не открываем: по спецификации сервер вправе ответить 405,
    # клиент после этого работает через POST.
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
async def mcp_delete():
    if not mcp_server.enabled():
        return _hidden()
    return Response(status_code=204)
