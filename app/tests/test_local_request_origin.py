"""CSRF-проверка Origin: чужая веб-страница не должна менять состояние.

Простой POST без preflight (form, text/plain, raw body) обходит CORS: именно
так чужой сайт мог поставить расширение или переписать файл. Своя оболочка,
расширения браузера и не-браузерные клиенты не блокируются.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import local_request  # noqa: E402


def test_чужой_сайт_отклоняется():
    assert local_request.origin_is_foreign(
        "https://evil.example", "127.0.0.1:8281") is True


def test_свой_хост_пропускается():
    assert local_request.origin_is_foreign(
        "http://192.168.1.5:8281", "192.168.1.5:8281") is False


def test_loopback_пропускается_на_любом_порту():
    # Расширения и их серверы живут на своих портах localhost.
    assert local_request.origin_is_foreign(
        "http://localhost:8000", "127.0.0.1:8281") is False


def test_оболочка_и_расширения_браузера_не_трогаются():
    for origin in ("file://", "app://infinity", "chrome-extension://abc", "null", ""):
        assert local_request.origin_is_foreign(origin, "127.0.0.1:8281") is False


def test_подделка_origin_с_чужим_хостом_не_проходит():
    assert local_request.origin_is_foreign(
        "https://127.0.0.1.evil.example", "127.0.0.1:8281") is True
