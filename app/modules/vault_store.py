#!/usr/bin/env python3
"""Локальное хранилище паролей, FAQ и глобального буфера обмена.

Всё лежит в <корень>/data (портативная сборка) или в ~/.infinity при
невозможности определить корень. Файлы пишутся атомарно: сбой во время
сохранения не должен оставить пустой JSON и «потерять» пароли.
"""
import json
import os
import tempfile
import threading
import time
import uuid
import swallow


def data_dir():
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('vault_store.data_dir:L24')
        return os.path.expanduser("~/.infinity")


def _path(name):
    return os.path.join(data_dir(), name)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        swallow.note('vault_store._read_json:L38')
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            swallow.note('vault_store._write_json:L54')
            pass


# ── Пароли ──────────────────────────────────────────────────────────────────

_VAULT_LOCK = threading.RLock()
_VAULT_FILE = "passwords.json"


def _vault_path():
    return _path(_VAULT_FILE)


def load_passwords():
    rows = _read_json(_vault_path(), [])
    return rows if isinstance(rows, list) else []


def list_passwords(profile="", url=""):
    profile = str(profile or "")
    rows = [r for r in load_passwords() if str(r.get("profile") or "") == profile]
    if url:
        host = _host(url)
        if host:
            rows = [r for r in rows if _host(r.get("url") or "") == host or
                    (r.get("url") or "").strip() == url.strip()]
    return rows


def _host(url):
    try:
        from urllib.parse import urlparse
        u = url if "://" in url else "https://" + url
        return (urlparse(u).hostname or "").lower()
    except Exception:
        swallow.note('vault_store._host:L90')
        return ""


def save_password(payload, profile="", password_id=None):
    payload = dict(payload or {})
    profile = str(profile or "")
    title = str(payload.get("title") or "").strip()
    url = str(payload.get("url") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    notes = str(payload.get("notes") or "")
    if not (title or url or username):
        raise ValueError("Нужно заполнить название, сайт или логин")
    with _VAULT_LOCK:
        rows = load_passwords()
        now = time.time()
        if password_id:
            for r in rows:
                if str(r.get("id")) == str(password_id):
                    if str(r.get("profile") or "") != profile:
                        raise PermissionError("Запись принадлежит другому профилю")
                    r.update({
                        "title": title or r.get("title", ""),
                        "url": url,
                        "username": username,
                        "password": password,
                        "notes": notes,
                        "updated": now,
                    })
                    _write_json(_vault_path(), rows)
                    return r
            raise KeyError("Пароль не найден")
        row = {
            "id": "pw_" + uuid.uuid4().hex[:16],
            "title": title or _host(url) or username,
            "url": url,
            "username": username,
            "password": password,
            "notes": notes,
            "profile": profile,
            "created": now,
            "updated": now,
        }
        rows.append(row)
        _write_json(_vault_path(), rows)
        return row


def delete_password(password_id, profile=""):
    profile = str(profile or "")
    with _VAULT_LOCK:
        rows = load_passwords()
        keep = []
        found = False
        for r in rows:
            if str(r.get("id")) == str(password_id) and str(r.get("profile") or "") == profile:
                found = True
                continue
            keep.append(r)
        if found:
            _write_json(_vault_path(), keep)
        return found


# ── FAQ панели уведомлений ──────────────────────────────────────────────────

DEFAULT_FAQ = """<h3>Горячие клавиши</h3>
<p><b>Shift + Space</b> — открыть палитру быстрого запроса к ИИ.</p>
<p><b>Ctrl + Shift + H</b> — скрыть или показать кнопки окна приложения.</p>
<p><b>Alt + перетаскивание</b> — переместить окно, если за него неудобно взяться: зажмите Alt и тяните окно за меню приложений.</p>
<h3>Рабочий стол</h3>
<p>Окна можно притягивать к краям экрана: к левой или правой стороне — на половину ширины, к верхней или нижней — на половину высоты, к углам — на четверть. У верхней кромки окно разворачивается на весь экран. Отключается в настройках → Интерфейс.</p>"""


def _faq_path():
    return _path("desktop_faq.json")


def load_faq():
    data = _read_json(_faq_path(), None)
    if isinstance(data, dict) and isinstance(data.get("html"), str):
        return data["html"]
    return DEFAULT_FAQ


def save_faq(html):
    html = str(html or "")
    _write_json(_faq_path(), {"html": html, "updated": time.time()})
    return html


# ── Глобальный буфер обмена ────────────────────────────────────────────────

_CLIP_LOCK = threading.RLock()
_CLIP_FILE = "clipboard_global.json"
_CLIP_MAX = 200
_CLIP_MAX_LEN = 100000
_ACTIVE_PROFILE = ""
_CLIP_PAUSED = False


def _clip_path():
    return _path(_CLIP_FILE)


def load_global_clipboard():
    rows = _read_json(_clip_path(), [])
    return rows if isinstance(rows, list) else []


def add_global_clipboard(text, profile=None):
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > _CLIP_MAX_LEN:
        text = text[:_CLIP_MAX_LEN] + "\n… (обрезано)"
    with _CLIP_LOCK:
        rows = load_global_clipboard()
        if rows and rows[-1].get("text") == text:
            return None
        pid = _ACTIVE_PROFILE if profile is None else profile or ""
        if profile is None and not pid:
            # После перезапуска сервера монитор стартует раньше, чем рабочий
            # стол успеет сообщить профиль. Берём последний известный.
            pid = _read_active_profile()
        entry = {
            "id": "gclip_" + uuid.uuid4().hex[:16],
            "text": text,
            "time": time.time(),
            "profile": str(pid or ""),
        }
        rows.append(entry)
        if len(rows) > _CLIP_MAX:
            rows = rows[-_CLIP_MAX:]
        _write_json(_clip_path(), rows)
        return entry


def list_global_clipboard(profile="", limit=200):
    profile = str(profile or "")
    with _CLIP_LOCK:
        rows = [r for r in load_global_clipboard()
                if str(r.get("profile") or "") == profile]
    try:
        limit = max(1, min(int(limit or 200), _CLIP_MAX))
    except Exception:
        swallow.note('vault_store.list_global_clipboard:L237')
        limit = 200
    return rows[-limit:]


def clear_global_clipboard(profile=""):
    profile = str(profile or "")
    with _CLIP_LOCK:
        rows = [r for r in load_global_clipboard()
                if str(r.get("profile") or "") != profile]
        _write_json(_clip_path(), rows)
    return True


def set_active_profile(profile):
    global _ACTIVE_PROFILE
    pid = str(profile or "")
    if pid == _ACTIVE_PROFILE:
        return _ACTIVE_PROFILE
    _ACTIVE_PROFILE = pid
    try:
        _write_json(_path("clipboard_active.json"), {"profile": _ACTIVE_PROFILE})
    except OSError:
        swallow.note('vault_store.set_active_profile:L260')
        pass
    return _ACTIVE_PROFILE


def _read_active_profile():
    data = _read_json(_path("clipboard_active.json"), {})
    if isinstance(data, dict):
        return str(data.get("profile") or "")
    return ""


def get_active_profile():
    return _ACTIVE_PROFILE


def set_clipboard_paused(paused):
    global _CLIP_PAUSED
    _CLIP_PAUSED = bool(paused)
    return _CLIP_PAUSED


def clipboard_paused():
    return _CLIP_PAUSED
