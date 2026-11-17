"""Привязки глобальных клавиш: конфиг -> хук-поток.

Ключи привязываются только включённых модулей: при выключенном `screenshots`
сочетание не должно перехватываться вообще — нечего отбирать у системы то, что
приложение не собирается обрабатывать.

Сами обработчики регистрирует модуль-владелец (`hotkey_hub.hub().on(...)`),
здесь только таблица клавиш и применение изменений на лету.

Флаги модулей и сочетания читаются из конфига, а конфиг может быть поменян не
этим процессом (правка файла, другой профиль). `reconcile()` сверяет секции по
содержимому на диске и переприменяет привязки, поэтому включение модуля видно
сразу, без перезапуска сервера.
"""

import hashlib
import json
import logging
import threading

import hotkey_hub
import module_flags
import swallow

logger = logging.getLogger("infinity")

_LOCK = threading.RLock()

# Хеш секций конфига, которые мы уже применили; сверяется в reconcile().
_fingerprint = None

DEFAULT_HOTKEYS = {
    "screenshot.full": "ctrl+alt+s",
    "screenshot.region": "ctrl+alt+shift+s",
    "screenshot.window": "ctrl+alt+w",
    "screenshot.timer": "ctrl+alt+t",
    "automator.toggle": "ctrl+alt+a",
    "automator.panic": "ctrl+shift+break",
}

# Какое сочетание принадлежит какому модулю — по префиксу имени.
_MODULE_OF = {"screenshot": "screenshots", "automator": "automator"}


def _module_for(name: str) -> str:
    prefix = str(name).split(".")[0]
    return _MODULE_OF.get(prefix, "")


def current_bindings() -> dict:
    """Привязки с учётом конфига; выключенные модули исключены."""
    try:
        from config_module import get_config
        configured = (get_config() or {}).get("hotkeys")
    except Exception as e:
        logger.error("[hotkey] конфиг привязок не прочитан: %s", e)
        configured = {}
    configured = configured if isinstance(configured, dict) else {}
    out = {}
    for name, default_spec in DEFAULT_HOTKEYS.items():
        module = _module_for(name)
        if module and not module_flags.enabled(module):
            continue
        spec = str(configured.get(name) or default_spec).strip()
        out[name] = spec
    return out


def reconfigure() -> dict:
    """Применить привязки; при первом включении модуля поднять хук-поток."""
    hub = hotkey_hub.hub()
    bindings = current_bindings()
    hub.set_bindings(bindings)
    if bindings and not hub.snapshot()["running"]:
        started = hub.start()
        if not started.get("ok"):
            logger.warning("[hotkey] поток не поднят: %s", started.get("error"))
    elif not bindings and hub.snapshot()["running"]:
        hub.stop()
    return {"bindings": bindings, "hub": hub.snapshot()}


def _disk_fingerprint() -> str:
    """Хеш того, что влияет на привязки: секции конфига + активный профиль.

    Секции, а не весь конфиг: иначе любое постороннее изменение (ключ провайдера,
    путь индекса) заставило бы сбрасывать кэш и переставлять хук без причины.

    Активный профиль — частью отпечатка, а не только источником секций: у двух
    профилей секции {modules, hotkeys} могут оказаться одинаковыми (оба
    выключены), а привязки всё равно считаются по активному, и смена профиля
    обязана быть замечена. Без этого фактора switch_profile менял конфиг,
    сверка возвращала changed=False и хук оставался в состоянии прошлого
    профиля — вплоть до полностью снятых привязок.
    """
    from config_module import load_config
    cfg = load_config() or {}
    relevant = {k: cfg.get(k) for k in ("modules", "hotkeys")}
    try:
        import profiles_module as PM
        active = PM.get_active_id() or ""
    except Exception:
        swallow.note('hotkey_shell._disk_fingerprint:L102')
        active = ""
    blob = json.dumps([active, relevant], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def reconcile(force: bool = False) -> dict:
    """Переприменить привязки, если конфиг на диске разошёлся с процессом.

    Возвращает тот же словарь, что `reconfigure()`, с ключом "changed".
    Вызывается из GET-эндпоинтов привязок: они и так обращаются к хуку, а чтение
    конфига с диска — единственный способ увидеть правку вне этого процесса.
    """
    global _fingerprint
    with _LOCK:
        try:
            fingerprint = _disk_fingerprint()
        except Exception as e:
            logger.error("[hotkey] конфиг с диска не прочитан: %s", e)
            return {"bindings": current_bindings(),
                    "hub": hotkey_hub.hub().snapshot(), "changed": False}
        changed = force or fingerprint != _fingerprint
        _fingerprint = fingerprint
        if not changed:
            return {"bindings": current_bindings(),
                    "hub": hotkey_hub.hub().snapshot(), "changed": False}
        # Кэш конфига отстаёт от файла — обновляем его, иначе module_flags
        # по-прежнему читает старые флаги.
        from config_module import reload_config
        reload_config()
        applied = reconfigure()
        applied["changed"] = True
        logger.info("[hotkey] конфиг сверен, привязок: %d", len(applied["bindings"]))
        return applied

