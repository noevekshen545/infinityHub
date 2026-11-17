"""Что именно версионируется в v1: настройки и переписки профиля.

Источники намеренно перечислены здесь, а не в хранилище: движок VCS работает
с логическими путями, и позже можно добавить файлы проекта, не меняя формат
объектов и не трогая уже сделанные коммиты.

Логические пути:

* ``settings/config.json`` — конфиг профиля (включая флаги модулей);
* ``settings/frontend_state.json`` — состояние интерфейса профиля;
* ``chats/<имя>.json`` — файлы переписок из каталога чата профиля.

Все пути берутся для ЯВНО указанного профиля: глобальный «активный профиль»
сервера здесь не используется, иначе коммит одного пользователя снимал бы
данные другого.
"""

from __future__ import annotations

import os
import re
import time

import swallow

SETTINGS_CONFIG = "settings/config.json"
SETTINGS_STATE = "settings/frontend_state.json"
CHAT_PREFIX = "chats/"
MAX_FILE_BYTES = 8 * 1024 * 1024

_CHAT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")
_KNOWN_SETTINGS = (SETTINGS_CONFIG, SETTINGS_STATE)


class SourceError(ValueError):
    def __init__(self, message, status=400, code="source_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _pm():
    import profiles_module
    return profiles_module


def _chat_dir(profile_id: str) -> str:
    import profile_paths
    return profile_paths.dir_for(profile_id, "CHAT_DIR")


def _safe_chat_name(logical: str) -> str:
    name = logical[len(CHAT_PREFIX):]
    if not _CHAT_NAME_RE.match(name):
        raise SourceError("Недопустимое имя файла переписки: %r" % logical,
                          400, "invalid_path")
    return name


def paths(profile_id: str) -> dict:
    """Логический путь -> абсолютный путь для профиля."""
    pid = str(profile_id or "").strip()
    if not pid:
        raise SourceError("Не указан профиль", 400, "profile_required")
    PM = _pm()
    if not PM.profile_exists(pid):
        raise SourceError("Профиль не найден: %s" % pid, 404, "profile_not_found")
    out = {}
    for logical, maker in ((SETTINGS_CONFIG, PM._profile_config_path),
                           (SETTINGS_STATE, PM._profile_frontend_state_path)):
        try:
            out[logical] = maker(pid)
        except Exception as exc:
            swallow.note("vcs_sources.paths:%s" % logical, exc)
    try:
        chat_dir = _chat_dir(pid)
    except Exception as exc:
        swallow.note("vcs_sources.paths:chat_dir", exc)
        chat_dir = ""
    if chat_dir and os.path.isdir(chat_dir):
        for name in sorted(os.listdir(chat_dir)):
            if not _CHAT_NAME_RE.match(name):
                continue
            full = os.path.join(chat_dir, name)
            if os.path.isfile(full):
                out[CHAT_PREFIX + name] = full
    return out


def collect(profile_id: str) -> dict:
    """Снимок источников: ``{логический путь: bytes}``."""
    files, _skipped = collect_report(profile_id)
    return files


def collect_report(profile_id: str) -> tuple[dict, list]:
    """Снимок и список пропущенных файлов (слишком большие/нечитаемые)."""
    files = {}
    skipped = []
    for logical, full in paths(profile_id).items():
        try:
            size = os.path.getsize(full)
        except OSError:
            skipped.append({"path": logical, "reason": "нет доступа"})
            continue
        if size > MAX_FILE_BYTES:
            skipped.append({"path": logical, "reason": "больше %d МБ"
                            % (MAX_FILE_BYTES // (1024 * 1024))})
            continue
        try:
            with open(full, "rb") as fh:
                files[logical] = fh.read()
        except OSError as exc:
            swallow.note("vcs_sources.collect:%s" % logical, exc)
            skipped.append({"path": logical, "reason": "не читается"})
    return files, skipped


def _backup(profile_id: str, mapping: dict) -> str:
    """Копия текущих файлов перед перезаписью; возвращает каталог бэкапа."""
    PM = _pm()
    base = PM.get_profile_data_dir(profile_id)
    if not base:
        raise SourceError("Профиль не найден: %s" % profile_id, 404,
                          "profile_not_found")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = os.path.join(base, "vcs_backups", stamp)
    suffix = 0
    while os.path.exists(root):
        suffix += 1
        root = os.path.join(base, "vcs_backups", "%s_%d" % (stamp, suffix))
    for logical, full in mapping.items():
        if not os.path.isfile(full):
            continue
        target = os.path.join(root, *logical.split("/"))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(full, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        except OSError as exc:
            swallow.note("vcs_sources.backup:%s" % logical, exc)
    return root


def restore(profile_id: str, manifest: dict, *, prune: bool = False) -> dict:
    """Записать снимок в источники профиля, сделав резервную копию.

    ``prune=True`` убирает файлы переписок, которых нет в снимке, но не удаляет
    их безвозвратно: они переносятся в каталог бэкапа. Настройки не удаляются
    никогда — отсутствующий в старом коммите конфиг иначе «исчез бы» у профиля.
    """
    pid = str(profile_id or "").strip()
    if not pid:
        raise SourceError("Не указан профиль", 400, "profile_required")
    current = paths(pid)
    work = {}
    for logical in (manifest or {}):
        if logical in _KNOWN_SETTINGS:
            if logical not in current:
                raise SourceError("Не найден путь настройки: %s" % logical,
                                  500, "source_missing")
            work[logical] = current[logical]
        elif logical.startswith(CHAT_PREFIX):
            name = _safe_chat_name(logical)
            work[logical] = os.path.join(_chat_dir(pid), name)
        else:
            raise SourceError("Неизвестный источник: %s" % logical, 400,
                              "unknown_source")
    backup_dir = _backup(pid, work)
    written = unchanged = 0
    for logical, full in work.items():
        data = manifest[logical]
        if not isinstance(data, (bytes, bytearray)):
            raise SourceError("Содержимое %s не bytes" % logical, 400,
                              "invalid_blob")
        try:
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    if fh.read() == bytes(data):
                        unchanged += 1
                        continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".tmp-vcs"
            with open(tmp, "wb") as fh:
                fh.write(bytes(data))
            os.replace(tmp, full)
            written += 1
        except OSError as exc:
            swallow.note("vcs_sources.restore:%s" % logical, exc)
            raise SourceError("Не удалось записать %s: %s" % (logical, exc),
                              500, "write_failed")
    pruned = []
    if prune:
        chat_dir = _chat_dir(pid)
        keep = {logical for logical in work if logical.startswith(CHAT_PREFIX)}
        try:
            present = sorted(os.listdir(chat_dir))
        except OSError:
            present = []
        for name in present:
            logical = CHAT_PREFIX + name
            if not _CHAT_NAME_RE.match(name) or logical in keep:
                continue
            src = os.path.join(chat_dir, name)
            if not os.path.isfile(src):
                continue
            target = os.path.join(backup_dir, "pruned", *logical.split("/"))
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(src, target)
                pruned.append(logical)
            except OSError as exc:
                swallow.note("vcs_sources.prune:%s" % logical, exc)
    return {"written": written, "unchanged": unchanged,
            "pruned": pruned, "backup_dir": backup_dir}
