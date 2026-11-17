"""Общие помощники для эндпоинтов, перенесённых из legacy-сервера.

Контракты намеренно повторяют BaseHTTPRequestHandler:
- тело разбирается тем же кодом, что `_dispatch_POST` / `_parse_multipart`
  (JSON с `errors="replace"`, multipart через email);
- ошибка JSON отдаётся объектом `{"error": "Invalid JSON: ..."}` со статусом 400;
- при включённой аутентификации проверка и ответ 401 — как `_check_auth`.

Это нужно, чтобы фронтенд не заметил разницы после удаления моста.
"""

import base64
import hashlib
import json
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs

from fastapi.responses import JSONResponse
from fast_json import FastJSONResponse
import swallow


class BadJSON(Exception):
    """Тело не разобралось как JSON (legacy отвечал 400, а не 500)."""


def query(request):
    """Параметры строки запроса как parse_qs: пустые значения отбрасываются."""
    return parse_qs(request.url.query)


def q1(request, name, default=None):
    vals = query(request).get(name)
    return vals[0] if vals else default


async def read_body(request):
    """Вернуть (body, files) — ровно как `_dispatch_POST` в server_backup."""
    ctype = request.headers.get("content-type", "")
    raw = await request.body()
    if "multipart/form-data" in ctype:
        return parse_multipart(ctype, raw)
    text = raw.decode("utf-8", errors="replace") if raw else "{}"
    if not text:
        return {}, {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise BadJSON(f"Invalid JSON: {e}")
    return (data if isinstance(data, dict) else {}), {}


async def body_or_error(request):
    """(body, files, error_response) — error_response не None при битом JSON."""
    try:
        body, files = await read_body(request)
    except BadJSON as e:
        swallow.note('compat_helpers.body_or_error:L57', e)
        return None, None, bad_json(e)
    return body, files, None


def parse_multipart(content_type, raw_body):
    """Повтор `H._parse_multipart` (email-парсер, payload без filename — текст)."""
    body, files = {}, {}
    if not raw_body:
        return body, files
    head = (b"Content-Type: " + str(content_type).encode("utf-8", "replace")
            + b"\r\nMIME-Version: 1.0\r\n\r\n")
    msg = BytesParser(policy=policy.default).parsebytes(head + raw_body)
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_param("filename", header="content-disposition")
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        if filename:
            files[name] = {"filename": filename, "content": payload}
        else:
            body[name] = payload.decode("utf-8", errors="replace")
    return body, files


def j(data, status=200):
    return FastJSONResponse(content=data, status_code=status)


def bad_json(err):
    return j({"error": str(err)}, 400)


def auth_denied(request):
    """Повтор `H._check_auth`: токен в query, Basic Auth, иначе 401.

    FastAPI-маршруты (в отличие от BaseHTTPRequestHandler) проверку не делали:
    без неё после удаления моста перенесённые эндпоинты стали бы открытыми
    при включённой аутентификации.
    """
    import server

    if not getattr(server, "AUTH_ENABLED", False):
        return None
    token = request.query_params.get("token")
    want_token = getattr(server, "AUTH_TOKEN", "")
    if token and want_token and token == want_token:
        return None
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            username, password = decoded.split(":", 1)
            if username == getattr(server, "AUTH_USERNAME", ""):
                want_hash = getattr(server, "AUTH_PASSWORD_HASH", "")
                if want_hash:
                    if hashlib.sha256(password.encode()).hexdigest() == want_hash:
                        return None
                elif password == getattr(server, "AUTH_USERNAME", ""):
                    # Историческое поведение: без хеша пароль совпадает с логином.
                    return None
        except Exception:
            swallow.note('compat_helpers.auth_denied:L122')
            pass
    resp = JSONResponse({"error": "Требуется аутентификация"}, status_code=401)
    resp.headers["WWW-Authenticate"] = 'Basic realm="Infinity AI"'
    return resp
