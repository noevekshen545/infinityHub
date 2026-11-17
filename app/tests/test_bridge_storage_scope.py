"""Мост профильной изоляции хранилища не должен прятать общесистемную тему.

Скрипт `_STORAGE_SCOPE_JS` префиксует ключи localStorage/sessionStorage именем
профиля, чтобы переписки и настройки профилей не смешивались на общем origin.
Оформление при этом общесистемное: `theme.js` читает `infinity_theme` и
`infinity_accent`, поэтому эти ключи обязаны оставаться БЕЗ префикса — иначе в
именованном профиле агент видит пустое хранилище и рисует дефолтную палитру
вместо цветовой схемы приложения.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from hermes_module.integration import bridge  # noqa: E402

BODY = b"<html><head><title>agent</title></head><body>hi</body></html>"


def _injected(profile):
    return bridge._inject_storage_scope(BODY, profile).decode("utf-8")


def test_default_profile_untouched():
    assert bridge._inject_storage_scope(BODY, "default") == BODY
    assert bridge._inject_storage_scope(BODY, "") == BODY


def test_named_profile_marks_and_scopes_workspace():
    text = _injected("admin")
    assert "infinity-profile-storage-bridge" in text
    assert 'PREF="isearch:"+P+":"' in text
    assert 'getItem:function(n){return raw.getItem(K(n));}' in text
    assert 'setItem:function(n,v){return raw.setItem(K(n),String(v));}' in text


def test_theme_and_accent_keys_stay_global():
    text = _injected("admin")
    assert "var GLOBAL={infinity_theme:1,infinity_accent:1,html_theme:1};" in text
    assert "function K(n){return GLOBAL[n]?n:PREF+n;}" in text
    # Ни прямого PREF+n для этих ключей, ни старой формы без K(n) быть не должно.
    assert "raw.getItem(PREF+n)" not in text
    assert "raw.setItem(PREF+n" not in text
    assert "raw.removeItem(PREF+n)" not in text


def test_clear_and_length_stay_scoped():
    text = _injected("admin")
    # Очистка хранилища профиля не должна сносить общесистемную тему.
    assert "function has(k){return typeof k===\"string\"&&k.indexOf(PREF)===0;}" in text
    assert "clear:function(){for(var i=raw.length-1;i>=0;i--){var f=raw.key(i);if(has(f))raw.removeItem(f);}}" in text


def test_injection_is_idempotent():
    once = bridge._inject_storage_scope(BODY, "admin")
    twice = bridge._inject_storage_scope(once, "admin")
    assert twice == once


def test_body_without_head_or_body_untouched():
    raw = b"plain text"
    assert bridge._inject_storage_scope(raw, "admin") == raw
