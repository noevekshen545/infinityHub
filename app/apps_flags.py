"""Пер-приложенческие флаги (config.json → apps.per_app).

system_only      приложение не отдаётся в /api/apps: его нет в appstore,
                 на десктопе и у пользователей. Видно только в админ-панели.
no_auto_install  приложение есть в appstore, но не устанавливается на
                 десктоп автоматически; ручная установка работает.

Флаги живут в config.json рядом с настройками автообновления, чтобы не
трогать app.json самих приложений: обновлятор перезаписывает их из релизов.
"""

import logging
import re
import threading

logger = logging.getLogger("infinity")

DEFAULTS = {"system_only": False, "no_auto_install": False}
_ID_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,120}$")
_LOCK = threading.RLock()


def _check_id(app_id):
    app_id = str(app_id or "").strip()
    if not _ID_RE.match(app_id):
        raise ValueError("Некорректный id приложения")
    return app_id


def _apps_section():
    try:
        from config_module import get_config
        sec = (get_config() or {}).get("apps")
    except Exception as e:
        logger.error(f"apps_flags: {e}")
        return {}
    return sec if isinstance(sec, dict) else {}


def app_flags(app_id):
    """Флаги одного приложения; неизвестное — оба выключены."""
    app_id = str(app_id or "").strip()
    per = _apps_section().get("per_app")
    entry = per.get(app_id) if isinstance(per, dict) else None
    out = dict(DEFAULTS)
    if isinstance(entry, dict):
        for key in DEFAULTS:
            out[key] = bool(entry.get(key))
    return out


def all_flags():
    per = _apps_section().get("per_app")
    if not isinstance(per, dict):
        return {}
    out = {}
    for app_id, entry in per.items():
        if not isinstance(entry, dict):
            continue
        flags = {key: bool(entry.get(key)) for key in DEFAULTS}
        if flags["system_only"] or flags["no_auto_install"]:
            out[str(app_id)] = flags
    return out


def set_flags(app_id, system_only=None, no_auto_install=None):
    """Обновить флаги; None — не менять. Пустая запись удаляется из конфига."""
    app_id = _check_id(app_id)
    with _LOCK:
        from config_module import get_config, save_config, reload_config
        cfg = get_config() or {}
        sec = dict(cfg.get("apps") or {})
        per = dict(sec.get("per_app") or {})
        current = app_flags(app_id)
        if system_only is not None:
            current["system_only"] = bool(system_only)
        if no_auto_install is not None:
            current["no_auto_install"] = bool(no_auto_install)
        if current["system_only"] or current["no_auto_install"]:
            per[app_id] = dict(current)
        else:
            per.pop(app_id, None)
        sec["per_app"] = per
        cfg["apps"] = sec
        save_config(cfg)
        reload_config()
    return {"ok": True, "id": app_id, **app_flags(app_id)}
