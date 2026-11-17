"""Проверки локальности запроса: Origin для WebSocket и loopback-клиент.

Нужны, чтобы браузерная страница с чужого сайта не могла открыть WebSocket
к localhost (CSWSH): WS-рукопожатие не подчиняется CORS, и без проверки
Origin страница получала уведомления активного профиля.
"""

import ipaddress
import swallow


def _host_of(value):
    try:
        value = (value or "").strip()
        if not value:
            return ""
        if value.startswith("file://"):
            return "file"
        if value == "null":
            return "null"
        if "://" not in value:
            return ""
        rest = value.split("://", 1)[1]
        rest = rest.split("/", 1)[0]
        if rest.startswith("["):
            return rest[1:].split("]", 1)[0].lower()
        return (rest.rsplit(":", 1)[0] if ":" in rest else rest).lower()
    except Exception:
        swallow.note('local_request._host_of:L28')
        return ""


def host_header_host(value):
    """Хост из заголовка Host (может быть [::1]:8281)."""
    try:
        value = (value or "").strip()
        if value.startswith("["):
            return value[1:].split("]", 1)[0].lower()
        return (value.rsplit(":", 1)[0] if ":" in value else value).lower()
    except Exception:
        swallow.note('local_request.host_header_host:L40')
        return ""


def is_loopback_host(host):
    host = (host or "").strip().lower()
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        swallow.note('local_request.is_loopback_host:L51')
        return False


def origin_allowed(origin, host_header=""):
    """Origin допустим, если это локальное приложение или тот же хост.

    Пустой Origin — не браузер (тесты, локальные клиенты) — пропускаем.
    file:// и null — Electron/локальный файл. Иначе сравниваем хост Origin
    с заголовком Host: так работает и доступ через TLS-сайдкар/прокси.
    """
    origin = (origin or "").strip()
    if not origin:
        return True
    host = _host_of(origin)
    if host in ("file", "null"):
        return True
    if is_loopback_host(host):
        return True
    target = host_header_host(host_header)
    return bool(target) and host == target


def origin_is_foreign(origin, host_header=""):
    """Origin чужой веб-страницы (http/https с другим хостом).

    Для CSRF: браузер с внешнего сайта может послать простой POST без
    preflight, и CORS его не остановит. Не-http схемы (file://, app://,
    chrome-extension://) — это оболочка и расширения браузера, их не трогаем;
    не-браузерные клиенты Origin не присылают вовсе.
    """
    value = (origin or "").strip().lower()
    if not value.startswith(("http://", "https://")):
        return False
    return not origin_allowed(value, host_header)


def client_is_loopback(obj):
    try:
        client = getattr(obj, "client", None)
        host = (getattr(client, "host", "") or "") if client else ""
    except Exception:
        swallow.note('local_request.client_is_loopback:L79')
        host = ""
    return is_loopback_host(host)
