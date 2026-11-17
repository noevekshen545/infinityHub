# -*- coding: utf-8 -*-
"""Ссылки «Поделиться» для файлового менеджера.

Зачем отдельный модуль: file_manager.py и так 2300+ строк, а здесь своя
маленькая логика — реестр токенов и копирование в другой профиль.

Файл реестра: app/data/file_shares.json
{
  "<token>": {
    "path": "C:\\...\\file.pdf",
    "name": "file.pdf",
    "size": 12345,
    "created": 1793...,
    "expires": 1793...,        # 0 = бессрочно
    "owner": "admin"           # профиль, создавший ссылку
  }
}

Ссылка: /filemanager/share/<token> — отдаёт файл (inline для картинок/PDF,
иначе скачивание). Просроченная ссылка возвращает 410 и удаляется из реестра.
"""

import json
import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
import swallow

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DATA_DIR, "file_shares.json")
# Доступы (общий файл/папка) — отдельный реестр: у них есть получатель,
# статус принятия и право редактирования, а у ссылок ничего этого нет.
_GRANTS_FILE = os.path.join(_DATA_DIR, "file_grants.json")
_LOCK = threading.Lock()
MAX_SHARES = 2000
MAX_GRANTS = 2000
DEFAULT_DAYS = 7


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        swallow.note('file_share_module._load:L51')
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _write(data: Dict[str, Dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _FILE)


def _purge_expired(data: Dict[str, Dict[str, Any]]) -> bool:
    now = time.time()
    dead = [t for t, s in data.items()
            if s.get("expires") and float(s["expires"]) < now]
    for t in dead:
        data.pop(t, None)
    return bool(dead)


def create_link(path: str, owner: str = "", days: int = DEFAULT_DAYS) -> Dict[str, Any]:
    """Зарегистрировать ссылку на файл. days<=0 — без срока."""
    if not os.path.isfile(path):
        raise ValueError("Файл не найден")
    days = int(days if days is not None else DEFAULT_DAYS)
    now = time.time()
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    row = {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "size": os.path.getsize(path),
        "created": now,
        "expires": (now + days * 86400) if days > 0 else 0,
        "owner": str(owner or ""),
    }
    with _LOCK:
        data = _load()
        _purge_expired(data)
        if len(data) >= MAX_SHARES:
            # Самые старые ссылки уходят: реестр не должен расти бесконечно.
            old = sorted(data.items(), key=lambda kv: float(kv[1].get("created") or 0))
            for t, _s in old[:max(1, len(data) - MAX_SHARES + 1)]:
                data.pop(t, None)
        data[token] = row
        _write(data)
    return {"token": token, "url": "/filemanager/share/" + token,
            "name": row["name"], "size": row["size"],
            "expires": row["expires"]}


def resolve(token: str) -> Optional[Dict[str, Any]]:
    token = str(token or "")
    if not token:
        return None
    with _LOCK:
        data = _load()
        row = data.get(token)
        if not row:
            return None
        if row.get("expires") and float(row["expires"]) < time.time():
            data.pop(token, None)
            _write(data)
            return None
        if not os.path.isfile(row.get("path") or ""):
            data.pop(token, None)
            _write(data)
            return None
        return dict(row)


def list_links(owner: str = "") -> List[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        changed = _purge_expired(data)
        if changed:
            _write(data)
        rows = []
        for token, s in data.items():
            if owner and str(s.get("owner") or "") != str(owner):
                continue
            rows.append({
                "token": token,
                "url": "/filemanager/share/" + token,
                "name": s.get("name") or os.path.basename(s.get("path") or ""),
                "path": s.get("path") or "",
                "size": s.get("size") or 0,
                "created": s.get("created") or 0,
                "expires": s.get("expires") or 0,
                # Файл могли удалить/переименовать: ссылка тогда уже не работает,
                # но запись остаётся — в UI показываем это, а не молчим.
                "missing": not os.path.isfile(s.get("path") or ""),
            })
    rows.sort(key=lambda r: -(r["created"] or 0))
    return rows


def revoke(token: str) -> bool:
    with _LOCK:
        data = _load()
        if str(token) not in data:
            return False
        data.pop(str(token), None)
        _write(data)
    return True


def _unique_dest(dest_dir: str, name: str) -> str:
    stem, ext = os.path.splitext(name)
    dest = os.path.join(dest_dir, name)
    i = 2
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{stem} ({i}){ext}")
        i += 1
    return dest


def shared_dir(profile_id: str) -> str:
    """Папка «Shared» получателя — внутри его КОРНЕВОЙ папки профиля.

    Раньше копии ложились в `<профиль>/data/Shared`, а файловый менеджер
    открывает `<профиль>/root`: принятый файл существовал, но в интерфейсе
    его не было. Теперь Shared — обычная папка домашнего каталога профиля,
    поэтому видна в проводнике и открывается установленными программами
    теми же действиями, что и любой другой файл.
    """
    import profiles_module as PM
    if not PM.profile_exists(profile_id):
        raise ValueError("Профиль не найден")
    root = PM.get_profile_root(profile_id) or PM._profile_data_path(profile_id)
    dest = os.path.join(root, "Shared")
    os.makedirs(dest, exist_ok=True)
    return dest


def copy_to_profile(path: str, profile_id: str, subdir: str = "Shared") -> str:
    """Скопировать файл или папку в папку «Shared» другого профиля."""
    if not os.path.isfile(path) and not os.path.isdir(path):
        raise ValueError("Файл не найден")
    if not profile_id:
        raise ValueError("Не указан профиль")
    dest_dir = shared_dir(profile_id) if subdir == "Shared" else os.path.join(
        shared_dir(profile_id), subdir)
    dest = _unique_dest(dest_dir, os.path.basename(path.rstrip("\\/")))
    if os.path.isdir(path):
        shutil.copytree(path, dest)
    else:
        shutil.copy2(path, dest)
        # Копия — не общая папка: файл лежит в «Shared» как обычный файл.
    return dest


def _same_volume(a: str, b: str) -> bool:
    try:
        return os.path.splitdrive(os.path.abspath(a))[0].lower() == \
            os.path.splitdrive(os.path.abspath(b))[0].lower()
    except Exception:
        swallow.note('file_share_module._same_volume:L211')
        return False


def _link_or_copy(path: str, dest: str) -> str:
    """Положить файл в Shared: хардлинк, если том тот же, иначе копия.

    Хардлинк — это тот же файл (общий inode): правки получателя сразу видны
    владельцу, а удаление записи в Shared не трогает оригинал. На разных
    томах хардлинк невозможен, поэтому там копия — она помечается kind=copy.
    """
    if _same_volume(path, dest):
        try:
            os.link(path, dest)
            return "hardlink"
        except OSError:
            swallow.note('file_share_module._link_or_copy:L227')
            pass
    shutil.copy2(path, dest)
    return "copy"


def _drop_shared_entry(shared_path: str, kind: str) -> None:
    """Убрать запись из Shared, не задевая оригинал владельца.

    Только файлы: удаление хардлинка/копии не трогает содержимое оригинала,
    а рекурсивное удаление папки доступа могло бы стереть чужие файлы.
    """
    if not shared_path:
        return
    try:
        if kind in ("hardlink", "copy") and os.path.isfile(shared_path):
            os.remove(shared_path)
    except OSError:
        swallow.note('file_share_module._drop_shared_entry:L245')
        pass


def materialize_grant(grant_id: str, row: Dict[str, Any]) -> str:
    """Показать принятый общий файл в Shared получателя. Возвращает путь.

    Папки не материализуем: junction в Shared можно удалить из файлового
    менеджера рекурсивно, вместе с файлами владельца. Доступ к папке
    остаётся в окне «Доступы ко мне» и просматривается оттуда.
    """
    if row.get("is_dir"):
        return ""
    path = row.get("path") or ""
    if not os.path.isfile(path):
        return ""
    recipient = str(row.get("recipient") or "")
    if not recipient:
        return ""
    try:
        dest_dir = shared_dir(recipient)
        dest = _unique_dest(dest_dir, row.get("name") or os.path.basename(path))
        kind = _link_or_copy(path, dest)
    except Exception:
        swallow.note('file_share_module.materialize_grant:L269')
        return ""
    try:
        with _LOCK:
            data = _load_grants()
            r = data.get(str(grant_id))
            if r and r.get("status") == "accepted":
                r["shared_path"] = dest
                r["shared_kind"] = kind
                _write_grants(data)
                return dest
        # Грант уже отозван/отклонён, пока копировали — убираем запись.
        _drop_shared_entry(dest, kind)
    except Exception:
        swallow.note('file_share_module.materialize_grant:L283')
        pass
    return ""


# ---- Доступы: один и тот же файл/папка у владельца и получателя ----------

def _load_grants() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_GRANTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        swallow.note('file_share_module._load_grants:L295')
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _write_grants(data: Dict[str, Dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = _GRANTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _GRANTS_FILE)


def create_grant(path: str, owner: str, recipient: str,
                 can_edit: bool = False) -> Dict[str, Any]:
    """Выдать доступ к файлу/папке. Получатель должен его принять."""
    if not os.path.isfile(path) and not os.path.isdir(path):
        raise ValueError("Файл не найден")
    if not recipient:
        raise ValueError("Не указан профиль")
    now = time.time()
    gid = uuid.uuid4().hex
    row = {
        "path": os.path.abspath(path),
        "name": os.path.basename(path.rstrip("\\/")),
        "is_dir": os.path.isdir(path),
        "owner": str(owner or ""),
        "recipient": str(recipient),
        "can_edit": bool(can_edit),
        "created": now,
        "status": "pending",
        "responded": 0,
    }
    with _LOCK:
        data = _load_grants()
        if len(data) >= MAX_GRANTS:
            dead = [g for g, r in data.items()
                    if r.get("status") in ("declined", "revoked")]
            for g in dead[:max(1, len(data) - MAX_GRANTS + 1)]:
                data.pop(g, None)
        data[gid] = row
        _write_grants(data)
    return {"id": gid, **{k: row[k] for k in
                          ("name", "is_dir", "can_edit", "created", "status")}}


def get_grant(grant_id: str, alive: bool = True) -> Optional[Dict[str, Any]]:
    gid = str(grant_id or "")
    if not gid:
        return None
    with _LOCK:
        row = _load_grants().get(gid)
    if not row:
        return None
    if row.get("status") in ("revoked", "declined"):
        return None
    # Файл могли удалить после выдачи доступа.
    if alive and not os.path.exists(row.get("path") or ""):
        return None
    return dict(row)


def grant_meta(grant_id: str) -> Optional[Dict[str, Any]]:
    row = get_grant(grant_id)
    if not row:
        return None
    try:
        size = 0 if row.get("is_dir") else os.path.getsize(row["path"])
    except OSError:
        swallow.note('file_share_module.grant_meta:L366')
        size = 0
    return {"id": grant_id, "name": row["name"], "is_dir": bool(row.get("is_dir")),
            "size": size, "can_edit": bool(row.get("can_edit")),
            "owner": row.get("owner") or "", "recipient": row.get("recipient") or "",
            "status": row.get("status") or "", "created": row.get("created") or 0,
            "shared_path": row.get("shared_path") or "",
            "shared_kind": row.get("shared_kind") or ""}


def grant_target(grant_id: str) -> Optional[str]:
    """Путь на диске, к которому выдан доступ (для file/dir-роутов)."""
    row = get_grant(grant_id)
    return row["path"] if row else None


def resolve_sub(root: str, sub: str) -> Optional[str]:
    """Безопасно разрешить вложенный путь внутри папки доступа."""
    root_abs = os.path.abspath(root)
    sub = str(sub or "").replace("\\", "/").lstrip("/")
    target = os.path.abspath(os.path.join(root_abs, sub)) if sub else root_abs
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def list_grants(owner: str = "", recipient: str = "",
                statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    with _LOCK:
        data = _load_grants()
    rows = []
    for gid, r in data.items():
        if owner and str(r.get("owner") or "") != str(owner):
            continue
        if recipient and str(r.get("recipient") or "") != str(recipient):
            continue
        if statuses and str(r.get("status") or "") not in statuses:
            continue
        alive = os.path.exists(r.get("path") or "")
        rows.append({
            "id": gid,
            "name": r.get("name") or os.path.basename(r.get("path") or ""),
            "is_dir": bool(r.get("is_dir")),
            "owner": r.get("owner") or "",
            "recipient": r.get("recipient") or "",
            "can_edit": bool(r.get("can_edit")),
            "created": r.get("created") or 0,
            "status": r.get("status") or "",
            "missing": not alive,
            "shared_path": r.get("shared_path") or "",
            "shared_kind": r.get("shared_kind") or "",
        })
    rows.sort(key=lambda x: -(x.get("created") or 0))
    return rows


def respond_grant(grant_id: str, recipient: str, accept: bool) -> bool:
    gid = str(grant_id or "")
    with _LOCK:
        data = _load_grants()
        row = data.get(gid)
        if not row or str(row.get("recipient") or "") != str(recipient or ""):
            return False
        if row.get("status") not in ("pending", "accepted"):
            return False
        row["status"] = "accepted" if accept else "declined"
        row["responded"] = time.time()
        if not accept:
            _drop_shared_entry(row.get("shared_path") or "",
                               row.get("shared_kind") or "")
            row["shared_path"] = ""
            row["shared_kind"] = ""
        _write_grants(data)
        snapshot = dict(row)
    if accept and not snapshot.get("shared_path"):
        materialize_grant(gid, snapshot)
    return True


def revoke_grant(grant_id: str, owner: str) -> bool:
    gid = str(grant_id or "")
    with _LOCK:
        data = _load_grants()
        row = data.get(gid)
        if not row:
            return False
        if owner and str(row.get("owner") or "") != str(owner):
            return False
        _drop_shared_entry(row.get("shared_path") or "",
                           row.get("shared_kind") or "")
        row["shared_path"] = ""
        row["shared_kind"] = ""
        row["status"] = "revoked"
        row["responded"] = time.time()
        _write_grants(data)
    return True
