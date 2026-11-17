# -*- coding: utf-8 -*-
"""Быстрые заметки мини-приложения (Shift+Space).

Отдельное лёгкое хранилище: короткая запись не должна открывать полный
редактор заметок. Кнопка «В заметки» отправляет запись в обычный модуль
через server.notes_save_article.

Файл: app/data/quick_notes.json
Формат: {"profiles": {"<profile_id>": [note, ...]}}
note = {id, text, color, pinned, created, updated}
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
import swallow

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DATA_DIR, "quick_notes.json")
_LOCK = threading.Lock()
MAX_TEXT = 20000
MAX_NOTES = 500

_COLORS = ("", "yellow", "green", "blue", "pink")


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def _load() -> Dict[str, List[Dict[str, Any]]]:
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        swallow.note('quick_notes_module._load:L38')
        return {"profiles": {}}
    # Старый/ручной формат — просто список записей.
    if isinstance(data, list):
        return {"profiles": {"default": [n for n in data if isinstance(n, dict)]}}
    if not isinstance(data, dict):
        return {"profiles": {}}
    profs = data.get("profiles")
    if not isinstance(profs, dict):
        profs = {}
    clean: Dict[str, List[Dict[str, Any]]] = {}
    for pid, notes in profs.items():
        if isinstance(notes, list):
            clean[str(pid)] = [n for n in notes if isinstance(n, dict)]
    return {"profiles": clean}


def _write(data: Dict[str, Any]) -> None:
    _ensure_dir()
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _FILE)


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return text[:MAX_TEXT]


def _note_out(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(n.get("id") or ""),
        "text": str(n.get("text") or ""),
        "color": str(n.get("color") or ""),
        "pinned": bool(n.get("pinned")),
        "created": float(n.get("created") or 0),
        "updated": float(n.get("updated") or 0),
    }


def list_notes(profile_id: str) -> List[Dict[str, Any]]:
    with _LOCK:
        notes = _load()["profiles"].get(str(profile_id or "default"), [])
        rows = [_note_out(n) for n in notes]
    rows.sort(key=lambda n: (0 if n["pinned"] else 1, -n["updated"]))
    return rows


def get_note(profile_id: str, note_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        notes = _load()["profiles"].get(str(profile_id or "default"), [])
        for n in notes:
            if str(n.get("id")) == str(note_id):
                return _note_out(n)
    return None


def save_note(profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(profile_id or "default")
    text = _clean_text(payload.get("text"))
    if not text.strip() and not payload.get("id"):
        raise ValueError("Пустая заметка")
    note_id = str(payload.get("id") or "").strip()
    now = time.time()
    with _LOCK:
        data = _load()
        notes = data["profiles"].setdefault(pid, [])
        if note_id:
            target = None
            for n in notes:
                if str(n.get("id")) == note_id:
                    target = n
                    break
            if target is None:
                raise ValueError("Заметка не найдена")
            if "text" in payload:
                target["text"] = text
            if "color" in payload and payload.get("color") in _COLORS:
                target["color"] = payload.get("color")
            if "pinned" in payload:
                target["pinned"] = bool(payload.get("pinned"))
            target["updated"] = now
            result = _note_out(target)
        else:
            if len(notes) >= MAX_NOTES:
                raise ValueError("Слишком много быстрых заметок")
            note = {
                "id": uuid.uuid4().hex[:12],
                "text": text,
                "color": payload.get("color") if payload.get("color") in _COLORS else "",
                "pinned": bool(payload.get("pinned")),
                "created": now,
                "updated": now,
            }
            notes.append(note)
            result = _note_out(note)
        _write(data)
    return result


def delete_note(profile_id: str, note_id: str) -> bool:
    pid = str(profile_id or "default")
    with _LOCK:
        data = _load()
        notes = data["profiles"].get(pid, [])
        left = [n for n in notes if str(n.get("id")) != str(note_id)]
        if len(left) == len(notes):
            return False
        data["profiles"][pid] = left
        _write(data)
    return True
