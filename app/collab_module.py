#!/usr/bin/env python3
"""
Collab Module — совместное редактирование заметок Infinity AI Search System.

Last-write-wins модель: сервер держит актуальный текст заметки, клиенты
шлют полный текст, сервер распределяет строки по авторам (дифф по строкам)
и рассылает {text, segments, seq} всем участникам комнаты.

Также здесь живёт сигналинг WebRTC (screen share) — сообщения rtc
ретранслируются между участниками через коллаб-WebSocket.

Права:
- владелец: тот, кто создал комнату (профиль запроса, а не «активный профиль сервера»)
- приглашённые профили: запись в share_index типа 'note_collab' (с токеном)
- гости по ссылке: любой, у кого есть token (полный доступ)
"""

import os
import json
import time
import uuid
import bisect
import difflib
import asyncio
import queue
import threading
import logging
import swallow

logger = logging.getLogger("infinity")

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_COLLAB_DIR = os.path.join(_SERVER_DIR, "data", "collab")

# Палитра цветов авторов по порядку входа в комнату
PALETTE = [
    "#e5484d",  # красный
    "#3b82f6",  # синий
    "#22c55e",  # зелёный
    "#f59e0b",  # оранжевый
    "#a855f7",  # фиолетовый
    "#14b8a6",  # бирюзовый
    "#ec4899",  # розовый
    "#f97316",  # жёлто-оранжевый
    "#84cc16",  # лаймовый
    "#6366f1",  # индиго
]

ROOMS = {}
ROOMS_LOCK = threading.Lock()

# Удалённые комнаты (зеркала на стороне гостя из внешней сети)
_REMOTE_ROOMS = {}
_REMOTE_LOCK = threading.Lock()

# Очередь публикации событий на каналы координации (фоновый поток)
_PUB_QUEUE = queue.Queue()
_PUB_THREAD = None
_PUB_THREAD_LOCK = threading.Lock()
_REMOTE_SUBS_READY = False

# Мост для рассылки локальным WS-клиентам из потока поллинга координации
_EVENT_LOOP = None
_BROADCAST_CORO = None


def set_loop(loop):
    global _EVENT_LOOP
    _EVENT_LOOP = loop


def set_broadcast_coro(fn):
    """fn(nid, event) -> корутина рассылки локальным клиентам комнаты."""
    global _BROADCAST_CORO
    _BROADCAST_CORO = fn


def _broadcast_local(nid, event):
    loop = _EVENT_LOOP
    if not loop or not _BROADCAST_CORO:
        return
    try:
        loop.call_soon_threadsafe(_run_broadcast, nid, event)
    except Exception:
        swallow.note('collab_module._broadcast_local:L84')
        pass


def _run_broadcast(nid, event):
    try:
        asyncio.ensure_future(_BROADCAST_CORO(nid, event))
    except Exception:
        swallow.note('collab_module._run_broadcast:L92')
        pass


def _presence_payload(room):
    return {"type": "presence",
            "users": {u: {"name": v["name"], "color": v["color"],
                          "online": bool(v["online"])}
                      for u, v in room["users"].items()}}

# Передовые события для WS-клиентов комнаты: (nid, uid) -> asyncio.Queue.
# Используются админ-роутами (kick/set-role), чтобы HTTP-запрос владельца
# закрыл соединение участника или обновил его права.
CONTROL_QUEUES = {}
CONTROL_QUEUES_LOCK = threading.Lock()


def _register_control(nid, uid, q):
    with CONTROL_QUEUES_LOCK:
        CONTROL_QUEUES[(nid, uid)] = q


def _unregister_control(nid, uid):
    with CONTROL_QUEUES_LOCK:
        CONTROL_QUEUES.pop((nid, uid), None)


def push_control(nid, uid, event):
    with CONTROL_QUEUES_LOCK:
        q = CONTROL_QUEUES.get((nid, uid))
    if q:
        try:
            q.put_nowait(event)
            return True
        except Exception:
            swallow.note('collab_module.push_control:L127')
            pass
    return False

# Максимальный размер заметки для коллаборации (символов)
MAX_CONTENT_LEN = 2_000_000


def _meta_path(nid):
    return os.path.join(_COLLAB_DIR, f"{nid}.json")


def _load_meta(nid):
    try:
        if os.path.exists(_meta_path(nid)):
            with open(_meta_path(nid), encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        swallow.note('collab_module._load_meta:L145')
        pass
    return None


def _save_meta(nid, meta):
    try:
        os.makedirs(_COLLAB_DIR, exist_ok=True)
        with open(_meta_path(nid), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"collab: не удалось сохранить meta: {e}")


def _default_room(nid, token, owner_name, access_mode="invite", invited_rights="write"):
    return {
        "nid": nid,
        "token": token,
        "owner": owner_name,
        "owner_profile": None,
        "allowed_profiles": [],
        "write_profiles": [],         # профили с правом write (даже в режиме read)
        "moderators": [],             # profile_id модераторов (администрирование)
        "banned_profiles": [],        # profile_id исключённых (доступ закрыт даже с токеном)
        "access_mode": access_mode,       # "open" | "invite"
        "invited_rights": invited_rights, # "write" | "read" — права гостей/приглашённых
        "closed": False,                  # владелец закрыл доступ — токен недействителен
        "note_path": None,                # путь к файлу заметки владельца
        "requests": [],                   # [{req_id, profile_id, name, ts, status}]
        "title": "",
        "content": "",
        "users": {},        # uid -> {"name": str, "color": str, "online": bool}
        "segments": [],     # [{"s": int, "e": int, "uid": str, "color": str}]
        "line_owners": [],  # per-line: None | {"uid":..., "color":...}
        "messages": [],     # [{"ts": float, "uid": str, "name": str, "text": str}]
        "seq": 0,
        "clients": {},      # uid -> asyncio.Queue (заполняется main.py)
    }


def _notes_dir():
    """Каталог заметок профиля запроса; вне запроса — глобалы сервера.

    Чужой каталог здесь появляется, когда профиль «активный на десктопе»
    расходится с профилем cookie: заметка искалась (и при записи попадала)
    в чужой набор файлов.
    """
    try:
        import server, profile_paths
        return profile_paths.proxy(server).NOTES_DIR
    except Exception:
        swallow.note('collab_module._notes_dir:L196')
        return os.path.join(_SERVER_DIR, "notes")


def _find_note_file(nid):
    """Найти путь к файлу заметки (как notes_get_article)."""
    root = _notes_dir()
    try:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f == f"{nid}.json" or f.replace(".json", "") == nid:
                    return os.path.join(dirpath, f)
    except Exception:
        swallow.note('collab_module._find_note_file:L203')
        pass
    return None


def _profile_notes_dir(profile_id):
    """Каталог заметок профиля из его config.json (или искать в data/profiles)."""
    if not profile_id:
        return None
    try:
        import profiles_module as PM
        cfg = PM.get_profile_config(profile_id)
        if cfg and cfg.get("notes_dir"):
            nd = os.path.expanduser(cfg["notes_dir"])
            if os.path.isdir(nd):
                return nd
    except Exception:
        swallow.note('collab_module._profile_notes_dir:L220')
        pass
    try:
        # Раньше тут был жёсткий путь app/data/profiles/<id>/data/notes.
        # Но _SERVER_DIR — это каталог app/, а данные профилей лежат в
        # <корень>/data/profiles/. Такой fallback указывал в
        # несуществующее место, и при ошибке импорта server заметки
        # профиля становились недоступны. Берём путь из профильного
        # модуля — тот сам знает реальный DATA_DIR.
        import profiles_module as PM
        cand = os.path.join(PM.PROFILES_DIR, str(profile_id), "data", "notes")
        if os.path.isdir(cand):
            return cand
    except Exception:
        swallow.note('collab_module._profile_notes_dir:L234')
        pass
    return None


def _owner_note_abs(owner_profile, nid):
    """Абсолютный путь к файлу заметки в каталоге владельца."""
    nd = _profile_notes_dir(owner_profile)
    if nd:
        return os.path.join(nd, f"{nid}.json")
    return None


def _read_note(nid, room=None):
    """Прочитать заметку с диска: сначала каталог владельца (если известен),
    затем активный каталог."""
    if room and room.get("note_path"):
        try:
            with open(room["note_path"], encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            swallow.note('collab_module._read_note:L255')
            pass
    if room and room.get("owner_profile"):
        fp = _owner_note_abs(room["owner_profile"], nid)
        if fp and os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                swallow.note('collab_module._read_note:L264')
                pass
    fp = _find_note_file(nid)
    if not fp:
        return {"title": "", "content": "", "folder": "inbox"}
    try:
        with open(fp, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        swallow.note('collab_module._read_note:L273')
        return {"title": "", "content": "", "folder": "inbox"}


def _persist(nid, room, title, content):
    """Сохранить заметку на диск ВСЕГДА в каталог владельца комнаты
    (никаких копий в каталогах других профилей)."""
    try:
        note = _read_note(nid, room=room)
        folder = note.get("folder", "inbox")
        dest = None
        if room and room.get("note_path"):
            dest = room["note_path"]
        if not dest:
            ow = room.get("owner_profile") if room else None
            dest = _owner_note_abs(ow, nid) or _find_note_file(nid)
        if not dest:
            dest = os.path.join(_notes_dir(), f"{nid}.json")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        article = {
            "id": nid,
            "title": title,
            "content": content,
            "folder": folder,
            "tags": note.get("tags", []),
            "created": note.get("created", time.time()),
            "updated": time.time(),
        }
        with open(dest, "w", encoding="utf-8") as fh:
            json.dump(article, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"collab: не удалось сохранить заметку: {e}")


# ─── Дифф (LCS) и расчёт владельцев ────────────────────────────────────

# Если суммарная длина старого и нового текста больше этого порога,
# используем более быстрый построчный алгоритм (чтобы не висеть на char-diff).
CHAR_DIFF_LIMIT = 200_000


def _diff_line_owners(old_text, new_text, update_uid, old_owners):
    """Вернуть per-line авторов нового текста.

    Старые совпавшие строки сохраняют автора, изменённые/новые строки
    приписываются update_uid.
    """
    a = old_text.split("\n")
    b = new_text.split("\n")
    n, m = len(a), len(b)

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row = dp[i]
        row_next = dp[i + 1]
        ai = a[i]
        for j in range(m - 1, -1, -1):
            row[j] = row_next[j + 1] + 1 if ai == b[j] else max(row_next[j], row[j + 1])

    owners = [None] * m
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            if old_owners and i < len(old_owners) and old_owners[i]:
                owners[j] = old_owners[i]
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1

    return owners


def _line_starts(s):
    """Индексы начала каждой строки (позиция после каждого '\n')."""
    starts = [0]
    for i, ch in enumerate(s):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _old_owner_at(old_starts, old_owners, idx):
    if not old_owners:
        return None
    if idx < 0:
        return None
    line = bisect.bisect_right(old_starts, idx) - 1
    if 0 <= line < len(old_owners) and old_owners[line]:
        return old_owners[line]
    return None


def _owners_to_segments(text, owners, user_colors):
    """Слить per-line авторов в сегменты [{"s","e","uid","color"}]."""
    segments = []
    lines = text.split("\n")
    pos = 0
    idx = 0
    while idx < len(lines):
        owner = owners[idx] if idx < len(owners) else None
        if owner:
            color = owner.get("color") or user_colors.get(owner.get("uid"), PALETTE[0])
            seg = {"s": pos, "e": pos + len(lines[idx]), "uid": owner.get("uid"), "color": color}
            segments.append(seg)
        pos += len(lines[idx]) + 1
        idx += 1
    return segments


def _line_owners_from_segments(text, segments):
    """Восстановить per-line владельцев (максимальный по длине сегмент строки)."""
    lines = text.split("\n")
    result = []
    pos = 0
    for ln in lines:
        line_start = pos
        line_end = pos + len(ln)
        best = None
        best_len = -1
        for s in segments:
            seg_start = s["s"]
            seg_end = s["e"]
            if seg_end <= line_start:
                continue
            if seg_start >= line_end:
                break
            overlap = min(seg_end, line_end) - max(seg_start, line_start)
            if overlap > best_len:
                best_len = overlap
                best = {"uid": s["uid"], "color": s["color"]}
            if seg_start >= line_end:
                break
        result.append(best)
        pos = line_end + 1
    return result


def _char_segments(old_text, new_text, uid, my_color, old_owners, user_colors):
    """Посимвольный дифф: сегменты нового текста с сохранением владельцев.

    Совпадающие (equal) блоки сохраняют автора старого текста (с разбивкой
    по границам строк), вставленные/изменённые части приписываются uid.
    """
    segments = []
    if not old_text or not old_owners:
        # Нет старого контекста — вся заметка за текущим автором.
        return [{"s": 0, "e": len(new_text), "uid": uid, "color": my_color}]
    old_starts = _line_starts(old_text)
    sm = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)

    def add(start, end, owner):
        if start >= end or not owner:
            return
        color = owner.get("color") or user_colors.get(owner.get("uid"), PALETTE[0])
        seg = {"s": start, "e": end, "uid": owner.get("uid"), "color": color}
        if segments and segments[-1]["e"] == start and segments[-1]["uid"] == seg["uid"] and segments[-1]["color"] == seg["color"]:
            segments[-1]["e"] = end
        else:
            segments.append(seg)

    for opcode in sm.get_opcodes():
        tag, i1, i2, j1, j2 = opcode
        if tag == "equal":
            # Каждый символ equal-блока: old[i1 + (j - j1)] == new[j]
            prev_owner = None
            run_start = j1
            for off in range(i2 - i1):
                o = _old_owner_at(old_starts, old_owners, i1 + off)
                if off == 0:
                    prev_owner = o
                    run_start = j1
                elif o != prev_owner:
                    add(run_start, j1 + off, prev_owner)
                    prev_owner = o
                    run_start = j1 + off
            add(run_start, j2, prev_owner)
        elif tag in ("insert", "replace"):
            add(j1, j2, {"uid": uid, "color": my_color})
    return segments


def _compute_ownership(old_text, new_text, uid, old_owners, user_colors=None):
    """Вернуть (segments, line_owners) для нового текста.

    Для небольших текстов — посимвольный дифф (сохраняет авторов внутри строк),
    для больших — построчный LCS (быстрее).
    """
    my_color = (user_colors or {}).get(uid)
    if not my_color:
        my_color = PALETTE[0]
    if old_text == new_text:
        if old_owners:
            segs = _owners_to_segments(new_text, old_owners, user_colors or {})
            return segs, old_owners
        return [], old_owners
    if len(old_text) + len(new_text) > CHAR_DIFF_LIMIT:
        owners = _diff_line_owners(old_text, new_text, uid, old_owners)
        return _owners_to_segments(new_text, owners, user_colors or {}), owners
    segs = _char_segments(old_text, new_text, uid, my_color, old_owners, user_colors or {})
    line_owners = _line_owners_from_segments(new_text, segs)
    # Новые/необозначенные строки — текущий автор (слой last-write-wins)
    for i in range(len(line_owners)):
        if not line_owners[i]:
            line_owners[i] = {"uid": uid, "color": my_color}
    if not line_owners:
        line_owners = [{"uid": uid, "color": my_color}]
    return segs, line_owners


def _user_colors(room):
    return {uid: u["color"] for uid, u in room["users"].items()}


def _active_profile_id():
    """ID профиля, от имени которого идёт работа (или None).

    Вне запроса — как раньше, активный профиль сервера. Внутри запроса
    обязательно профиль cookie: «активный» переключается навигацией и на
    телефоне означал бы, что комнаты заводятся, приглашения и проверки прав
    уходят от чужого имени.
    """
    try:
        import profile_paths
        pid = profile_paths.current_profile_id()
        if pid:
            return pid
    except Exception:
        swallow.note('collab_module._active_profile_id:L509')
    try:
        import profiles_module
        active = profiles_module.get_active_profile()
        return active.get("id") if active else None
    except Exception:
        swallow.note('collab_module._active_profile_id:L515')
        return None


def _profile_role(room, pid):
    """Роль профиля в комнате. None — доступа нет (join будет отклонён).

    open (открыта для всех): ссылка-токен даёт доступ, роль — invited_rights;
        владелец и приглашённые всегда write.
    invite (только по приглашению): доступ только владельцу и приглашённым;
        роль приглашённых — invited_rights. Гость с токеном — доступа нет.

    Администрирование: исключённые (banned) не имеют доступа даже с токеном;
    модераторы всегда write и могут выгонять/менять права участников.

    Если комната закрыта владельцем — доступа нет ни у кого.
    """
    if not room:
        return None
    if room.get("closed"):
        return None
    owner = room.get("owner_profile")
    allowed = set(room.get("allowed_profiles") or [])
    write_set = set(room.get("write_profiles") or [])
    mod_set = set(room.get("moderators") or [])
    banned_set = set(room.get("banned_profiles") or [])
    invited_rights = room.get("invited_rights", "write") or "write"
    if pid:
        if pid in banned_set:
            return None
        if owner and pid == owner:
            return "write"
        if pid in mod_set:
            return "write"
        if pid in write_set:
            return "write"
        if pid in allowed:
            if room.get("access_mode") == "open":
                return "write"
            return invited_rights
    if room.get("access_mode") == "open":
        # Открытая комната: у кого есть токен — тот имеет право на invited_rights.
        return invited_rights
    return None


def _room_role(room):
    """Роль подключающегося клиента по профилю запроса (см. _active_profile_id)."""
    return _profile_role(room, _active_profile_id()) or "read"


# ─── API комнат ───────────────────────────────────────────────────────


def _ensure_room_from_meta(nid):
    """Восстановить комнату из меты (после рестарта сервера)."""
    meta = _load_meta(nid)
    if not meta:
        return None
    token = meta.get("token")
    room = _default_room(
        nid, token, meta.get("owner", "Гость"),
        access_mode=meta.get("access_mode", "invite"),
        invited_rights=meta.get("invited_rights", "write"),
    )
    room["owner_profile"] = meta.get("owner_profile")
    room["allowed_profiles"] = list(meta.get("allowed_profiles") or [])
    room["write_profiles"] = list(meta.get("write_profiles") or [])
    room["moderators"] = list(meta.get("moderators") or [])
    room["banned_profiles"] = list(meta.get("banned_profiles") or [])
    room["closed"] = bool(meta.get("closed"))
    room["title"] = meta.get("title", "")
    room["content"] = meta.get("content", "")
    room["segments"] = meta.get("segments") or []
    room["line_owners"] = meta.get("line_owners") or []
    room["messages"] = list(meta.get("messages") or [])
    room["requests"] = list(meta.get("requests") or [])
    room["seq"] = meta.get("seq", 0)
    room["remote_mode"] = bool(meta.get("remote_mode"))
    for uid, u in (meta.get("users") or {}).items():
        room["users"][uid] = {
            "name": u.get("name", "Гость"),
            "color": u.get("color", PALETTE[0]),
            "online": False,
            "profile_id": u.get("profile_id"),
        }
    note = _read_note(nid, room=room)
    if room["title"] == "":
        room["title"] = note.get("title", "")
    if room["content"] == "":
        room["content"] = note.get("content", "")
    return room


def start_room(nid, owner_name, access_mode="invite", invited_rights="write"):
    """Создать/получить комнату.

    Токен отдаётся только владельцу или приглашённому профилю активного
    пользователя (для приглашённых — при повторном старте комнаты).

    access_mode:
      "open"   — открыта для всех (ссылка-токен даёт доступ, роль = invited_rights)
      "invite" — только по приглашению (гости с токеном не входят)

    invited_rights: "write" | "read" — права гостей приглашённых
        (для invite-комнаты — права приглашённых, для open — права по ссылке).
    """
    pid = _active_profile_id()
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            room = _ensure_room_from_meta(nid)
            if room:
                ROOMS[nid] = room
        if room:
            role = _profile_role(room, pid)
            if not role:
                if room.get("closed"):
                    return {"ok": False, "error": "Совместный доступ к этой заметке закрыт", "code": "closed"}
                return {"ok": False, "error": "Доступ запрещён — заметка открыта только по приглашению", "code": "not_allowed"}
            if room.get("closed"):
                return {"ok": False, "error": "Совместный доступ к этой заметке закрыт", "code": "closed"}
            return {"ok": True, "token": room["token"], "created": False, "role": role}
        meta = _load_meta(nid) or {}
        if meta.get("closed"):
            return {"ok": False, "error": "Совместный доступ к этой заметке закрыт", "code": "closed"}
        token = meta.get("token") or uuid.uuid4().hex[:12]
        room = _default_room(nid, token, owner_name, access_mode, invited_rights)
        room["owner_profile"] = pid
        room["allowed_profiles"] = list(meta.get("allowed_profiles") or [])
        room["write_profiles"] = list(meta.get("write_profiles") or [])
        room["messages"] = list(meta.get("messages") or [])
        room["requests"] = list(meta.get("requests") or [])
        if access_mode in ("open", "invite") and invited_rights in ("write", "read"):
            room["access_mode"] = access_mode
            room["invited_rights"] = invited_rights
        note = _read_note(nid)
        room["title"] = note.get("title", "")
        room["content"] = note.get("content", "")
        fp = _find_note_file(nid)
        if fp:
            room["note_path"] = fp
        else:
            room["note_path"] = _owner_note_abs(pid, nid)
        ROOMS[nid] = room
        _persist_room(nid, room)
        return {"ok": True, "token": token, "created": True, "role": "write"}


def get_token(nid):
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        return room["token"] if room else None


def invite_profile(nid, token, target_profile_id, title="", profile_id=None):
    """Пригласить профиль в комнату (запись типа note_collab в share_index).

    Права приглашённого в комнате: для invite-комнат — invited_rights;
    для open-комнат приглашённый всегда write (владелец/приглашённые пишут).
    Кто может приглашать: владелец комнаты и модераторы (invite-режим) либо
    любой держатель токена (open-режим, где токен и есть доступ).
    """
    room = ROOMS.get(nid)
    if not room or room["token"] != token:
        return {"ok": False, "error": "Комната не найдена"}
    if room.get("closed"):
        return {"ok": False, "error": "Совместный доступ закрыт", "code": "closed"}
    try:
        import share_module
        if not profile_id:
            profile_id = _active_profile_id() or "default"
        owner_id = room.get("owner_profile")
        if owner_id and target_profile_id == owner_id:
            return {"ok": False, "error": "Это владелец заметки"}
        # Раньше право приглашать давал один лишь токен: кто его перехватил,
        # тот вписывал произвольные профили в allowed_profiles и переписывал
        # мету комнаты. В invite-режиме приглашают владелец и модераторы;
        # в open-режиме остаётся модель «ссылка-токен даёт доступ».
        # Комнаты без записанного владельца (созданы до правки / удалённые)
        # не блокируем: проверять роль не от чего.
        if owner_id and room.get("access_mode") != "open":
            if profile_id != owner_id and profile_id not in (room.get("moderators") or []):
                return {"ok": False, "error": "Приглашать может только владелец комнаты",
                        "code": "not_owner"}
        invited_rights = room.get("invited_rights", "write")
        permissions = "read" if (
            room.get("access_mode") == "invite" and invited_rights == "read"
        ) else "write"
        res = share_module.share_with_profile(
            profile_id, target_profile_id, "note_collab", nid, title or nid,
            permissions=permissions,
        )
        if not res.get("ok"):
            return res
        index = share_module._load_share_index(target_profile_id)
        for e in index.get("shared_with_me", []):
            if e.get("share_id") == res["share_id"]:
                e["collab_token"] = token
                e["collab_note_id"] = nid
                e["collab_access_mode"] = room.get("access_mode", "invite")
                e["collab_rights"] = invited_rights
        share_module._save_share_index(target_profile_id, index)
        # Запоминаем профиль в white-list комнаты.
        with ROOMS_LOCK:
            if room and target_profile_id not in room.get("allowed_profiles", []):
                room.setdefault("allowed_profiles", []).append(target_profile_id)
                _persist_room(nid, room)
        return {"ok": True, "share_id": res["share_id"]}
    except Exception as e:
        swallow.note('collab_module.invite_profile:L696', e)
        return {"ok": False, "error": str(e)}


def get_inbox(profile_id=None):
    """Приглашения для профиля запроса (записи note_collab в shared_with_me).

    Записи, чьей комнаты больше нет (мета-файл удалён), тихо чистятся,
    чтобы в уведомлениях не висели нерабочие приглашения."""
    try:
        if not profile_id:
            profile_id = _active_profile_id() or "default"
        import share_module
        index = share_module._load_share_index(profile_id)
        invites = [e for e in index.get("shared_with_me", []) if e.get("type") == "note_collab"]
        stale = []
        for e in invites:
            nid = e.get("collab_note_id") or e.get("item_id") or ""
            if nid and not os.path.exists(_meta_path(nid)) and not e.get("collab_remote"):
                stale.append(e)
        if stale:
            stale_nids = set()
            for e in stale:
                nid = e.get("collab_note_id") or e.get("item_id") or ""
                if nid:
                    stale_nids.add(nid)
            index["shared_with_me"] = [
                e for e in index.get("shared_with_me", [])
                if e.get("type") != "note_collab"
                or ((e.get("collab_note_id") or e.get("item_id") or "") not in stale_nids)
                or e.get("collab_remote")
            ]
            try:
                share_module._save_share_index(profile_id, index)
            except Exception:
                swallow.note('collab_module.get_inbox:L733')
                pass
            invites = [e for e in invites if
                       (e.get("collab_note_id") or e.get("item_id") or "")
                       not in stale_nids]
        return invites
    except Exception:
        swallow.note('collab_module.get_inbox:L740')
        return []


def get_status(nid, token=None):
    """Статус комнаты без подключения (только для того, у кого есть доступ)."""
    room = ROOMS.get(nid)
    if not room:
        room = _ensure_room_from_meta(nid)
        if room:
            ROOMS[nid] = room
    if not room:
        return {"active": False}
    pid = _active_profile_id()
    role = _profile_role(room, pid)
    if not role:
        return {"active": False, "locked": True,
                "closed": bool(room.get("closed")),
                "access_mode": room.get("access_mode"),
                "role": None}
    users = [{"uid": uid, "name": u["name"], "color": u["color"], "online": u["online"],
              "profile_id": u.get("profile_id")}
             for uid, u in room["users"].items()]
    result = {
        "active": True,
        "owner": room["owner"],
        "owner_profile": room.get("owner_profile"),
        "is_owner": bool(pid) and room.get("owner_profile") == pid,
        "is_moderator": bool(pid) and pid in (room.get("moderators") or []),
        "role": role,
        "closed": bool(room.get("closed")),
        "access_mode": room.get("access_mode"),
        "invited_rights": room.get("invited_rights"),
        "token": room["token"],
        "users": users,
        "moderators": list(room.get("moderators") or []),
    }
    if role == "write" and pid and room.get("owner_profile") == pid:
        result["requests"] = list(room.get("requests") or [])
    return result


def join_room(nid, token, uid, name, queue=None):
    """Подключить участника. Возвращает (ok, error, snapshot) + регистрирует очередь."""
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            room = _ensure_room_from_meta(nid)
            if room:
                ROOMS[nid] = room
        if not room:
            return False, "Комната не найдена", None
        if room["token"] != token:
            return False, "Неверный токен доступа", None
        pid = _active_profile_id()
        role = _profile_role(room, pid)
        if not role:
            if room.get("closed"):
                return False, "Совместный доступ к этой заметке закрыт владельцем", None
            if room.get("access_mode") == "invite":
                return False, "Доступ только по приглашению — запросите его у владельца", None
            return False, "Доступ запрещён", None
        room["clients"][uid] = queue
        existing = room["users"].get(uid)
        if existing:
            existing["online"] = True
            existing["name"] = name
            color = existing["color"]
            existing["profile_id"] = pid
        else:
            color = PALETTE[len([u for u in room["users"].values() if not u.get("_retired")]) % len(PALETTE)]
            room["users"][uid] = {"name": name, "color": color, "online": True,
                                  "profile_id": pid}
        my_role = _profile_role(room, pid)
        my_pid = pid
        snapshot = {
            "type": "welcome",
            "uid": uid,
            "token": room["token"],
            "owner": room["owner"],
            "owner_profile": room.get("owner_profile"),
            "profile_id": my_pid,
            "is_owner": bool(my_pid) and room.get("owner_profile") == my_pid,
            "is_moderator": bool(my_pid) and my_pid in (room.get("moderators") or []),
            "moderators": list(room.get("moderators") or []),
            "role": my_role,
            "access_mode": room.get("access_mode"),
            "invited_rights": room.get("invited_rights"),
            "closed": bool(room.get("closed")),
            "title": room["title"],
            "content": room["content"],
            "segments": room["segments"],
            "line_owners": room["line_owners"],
            "messages": room["messages"],
            "users": {u: {**v, "online": v["online"]} for u, v in room["users"].items()},
            "seq": room["seq"],
        }
        _publish_room_event(nid, {"type": "presence",
                                  "users": {u: {"name": v["name"], "color": v["color"],
                                                "online": bool(v["online"])}
                                            for u, v in room["users"].items()}})
        return True, None, snapshot


def leave_room(nid, uid):
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            return
        room["clients"].pop(uid, None)
        u = room["users"].get(uid)
        if u:
            u["online"] = False
        _persist_room(nid, room)
        _publish_room_event(nid, {"type": "presence",
                                  "users": {u: {"name": v["name"], "color": v["color"],
                                                "online": bool(v["online"])}
                                            for u, v in room["users"].items()}})


def _persist_room(nid, room):
    _save_meta(nid, {
        "nid": nid,
        "token": room["token"],
        "owner": room["owner"],
        "owner_profile": room.get("owner_profile"),
        "allowed_profiles": room.get("allowed_profiles") or [],
        "write_profiles": room.get("write_profiles") or [],
        "moderators": room.get("moderators") or [],
        "banned_profiles": room.get("banned_profiles") or [],
        "access_mode": room.get("access_mode", "invite"),
        "invited_rights": room.get("invited_rights", "write"),
        "closed": bool(room.get("closed")),
        "title": room["title"],
        "content": room["content"],
        "segments": room.get("segments") or [],
        "line_owners": room.get("line_owners") or [],
        "users": {uid: {"name": u["name"], "color": u["color"], "profile_id": u.get("profile_id")}
                  for uid, u in room["users"].items()},
        "messages": room.get("messages") or [],
        "requests": room.get("requests") or [],
        "seq": room.get("seq", 0),
        "remote_mode": bool(room.get("remote_mode")),
    })


def apply_update(nid, uid, text, title=None):
    """Применить последнюю версию текста (Last-write-wins), пересчитать авторов."""
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            return None
        if room.get("closed"):
            return None
        if len(text) > MAX_CONTENT_LEN:
            return None
        if uid not in room["users"]:
            uid = next(iter(room["users"]), uid)

        old_text = room["content"]
        user_colors = _user_colors(room)
        segments, owners = _compute_ownership(
            old_text, text, uid, room["line_owners"], user_colors)

        room["content"] = text
        room["line_owners"] = owners
        room["segments"] = segments
        room["seq"] += 1
        if title is not None:
            room["title"] = title

        event = {
            "type": "broadcast",
            "seq": room["seq"],
            "text": room["content"],
            "title": room["title"],
            "segments": room["segments"],
            "line_owners": room["line_owners"],
            "from": uid,
        }
        _persist(nid, room, room["title"], room["content"])
        _persist_room(nid, room)
        _publish_room_event(nid, event)
        return event


def send_chat(nid, uid, text):
    """Добавить сообщение в чат комнаты и вернуть его (или None)."""
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            return None
        if room.get("closed"):
            return None
        text = (text or "").strip()
        if not text or len(text) > 4000:
            return None
        msg = {
            "ts": time.time(),
            "uid": uid,
            "name": room["users"].get(uid, {}).get("name", "Гость"),
            "text": text,
        }
        room.setdefault("messages", []).append(msg)
        room["messages"] = room["messages"][-200:]
        _persist_room(nid, room)
        _publish_room_event(nid, {"type": "chat", **msg})
        return msg


def current_room(nid):
    return ROOMS.get(nid)


# ─── Закрытие доступа, отказ, запросы прав ───────────────────────────


def close_room(nid, token):
    """Владелец закрывает совместный доступ: токен недействителен,
    все приглашения отзываются, заметка больше никому не видна."""
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        if room["token"] != token:
            return {"ok": False, "error": "Неверный токен"}
        pid = _active_profile_id()
        owner = room.get("owner_profile")
        if pid and owner and pid != owner:
            return {"ok": False, "error": "Закрыть доступ может только владелец"}
        room["closed"] = True
        room["allowed_profiles"] = []
        room["write_profiles"] = []
        room["requests"] = []
        _persist_room(nid, room)
    _revoke_all_shares(nid, room)
    return {"ok": True}


def reopen_room(nid, token):
    """Владелец снова открывает доступ (токен прежний)."""
    with ROOMS_LOCK:
            room = ROOMS.get(nid)
            if not room:
                return {"ok": False, "error": "Комната не найдена"}
            if room["token"] != token:
                return {"ok": False, "error": "Неверный токен"}
            pid = _active_profile_id()
            owner = room.get("owner_profile")
            if pid and owner and pid != owner:
                return {"ok": False, "error": "Открыть доступ может только владелец"}
            room["closed"] = False
            _persist_room(nid, room)
    return {"ok": True, "token": room["token"]}


def _revoke_all_shares(nid, room):
    """Убрать ВСЕ входящие приглашения (note_collab) к заметке у всех профилей."""
    try:
        import share_module
        owner_id = room.get("owner_profile")
        for pid in [owner_id] + list(room.get("allowed_profiles") or []):
            if not pid:
                continue
            try:
                index = share_module._load_share_index(pid)
                before = len(index.get("shared_with_me", []))
                index["shared_with_me"] = [
                    e for e in index.get("shared_with_me", [])
                    if not (e.get("type") == "note_collab" and e.get("item_id") == nid)
                ]
                if len(index.get("shared_with_me", [])) != before:
                    share_module._save_share_index(pid, index)
                links = index.get("my_links", [])
                links_before = len(links)
                links = [
                    e for e in links
                    if not (e.get("type") == "note_collab" and e.get("item_id") == nid)
                ]
                if len(links) != links_before:
                    index["my_links"] = links
                    share_module._save_share_index(pid, index)
            except Exception:
                swallow.note('collab_module._revoke_all_shares:L1023')
                pass
    except Exception:
        swallow.note('collab_module._revoke_all_shares:L1026')
        pass


def decline_invite(nid, token, share_id=None):
    """Профиль отклоняет приглашение: убирает запись из инбокса
    и своё имя из белого списка комнаты."""
    room = ROOMS.get(nid)
    if not room:
        room = _ensure_room_from_meta(nid)
    with ROOMS_LOCK:
        if room:
            if token and room["token"] != token:
                return {"ok": False, "error": "Неверный токен"}
            pid = _active_profile_id()
            if pid and room.get("allowed_profiles"):
                room["allowed_profiles"] = [p for p in room["allowed_profiles"] if p != pid]
                room["write_profiles"] = [p for p in (room.get("write_profiles") or []) if p != pid]
                _persist_room(nid, room)
    try:
        import share_module
        pid = _active_profile_id()
        if pid:
            index = share_module._load_share_index(pid)
            before = len(index.get("shared_with_me", []))
            index["shared_with_me"] = [
                e for e in index.get("shared_with_me", [])
                if not (e.get("item_id") == nid and e.get("type") == "note_collab")
                and not (share_id and e.get("share_id") == share_id)
            ]
            if len(index.get("shared_with_me", [])) != before:
                share_module._save_share_index(pid, index)
    except Exception:
        swallow.note('collab_module.decline_invite:L1059')
        pass
    return {"ok": True}


def request_edit(nid, token, req_name=""):
    """Гость запрашивает право на редактирование у владельца."""
    room = ROOMS.get(nid)
    if not room:
        room = _ensure_room_from_meta(nid)
    if not room:
        return {"ok": False, "error": "Комната не найдена"}
    if room["token"] != token:
        return {"ok": False, "error": "Неверный токен"}
    pid = _active_profile_id()
    if not pid:
        return {"ok": False, "error": "Профиль не определён"}
    if room.get("closed"):
        return {"ok": False, "error": "Совместный доступ к этой заметке закрыт"}
    role = _profile_role(room, pid)
    if role == "write":
        return {"ok": True, "already": True, "role": "write"}
    with ROOMS_LOCK:
        room = ROOMS.get(nid) or _ensure_room_from_meta(nid)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        existing = next((r for r in room.get("requests") or [] if r.get("profile_id") == pid), None)
        if existing and existing.get("status") == "pending":
            return {"ok": True, "pending": True, "req_id": existing["req_id"]}
        if existing:
            room["requests"] = [r for r in room.get("requests") or [] if r.get("profile_id") != pid]
        req = {
            "req_id": uuid.uuid4().hex[:8],
            "profile_id": pid,
            "name": req_name or "",
            "ts": time.time(),
            "status": "pending",
        }
        room.setdefault("requests", []).append(req)
        _persist_room(nid, room)
    return {"ok": True, "req_id": req["req_id"]}


def get_requests(nid, token):
    """Заявки на доступ (только владелец)."""
    room = ROOMS.get(nid)
    if not room:
        room = _ensure_room_from_meta(nid)
    if not room or room["token"] != token:
        return {"ok": False, "error": "Нет доступа"}
    pid = _active_profile_id()
    if room.get("owner_profile") and pid != room.get("owner_profile"):
        return {"ok": False, "error": "Только владелец"}
    return {"ok": True, "requests": list(room.get("requests") or [])}


def request_handle(nid, token, req_id, accept):
    """Владелец принимает/отклоняет заявку на редактирование."""
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            room = _ensure_room_from_meta(nid)
        if not room or room["token"] != token:
            return {"ok": False, "error": "Комната не найдена"}
        pid = _active_profile_id()
        if room.get("owner_profile") and pid != room.get("owner_profile"):
            return {"ok": False, "error": "Только владелец может обработать заявку"}
        req = next((r for r in room.get("requests") or [] if r.get("req_id") == req_id), None)
        if not req:
            return {"ok": False, "error": "Заявка не найдена"}
        req["status"] = "accepted" if accept else "declined"
        if accept:
            target = req["profile_id"]
            if target not in room.get("allowed_profiles", []):
                room.setdefault("allowed_profiles", []).append(target)
            room.setdefault("write_profiles", [])
            if target not in room["write_profiles"]:
                room["write_profiles"].append(target)
            _persist_room(nid, room)
            import share_module, profiles_module
            tname = ""
            try:
                for p in profiles_module.list_profiles():
                    if p["id"] == target:
                        tname = p.get("name", "")
                        break
            except Exception:
                swallow.note('collab_module.request_handle:L1146')
                pass
            share_res = share_module.share_with_profile(
                room.get("owner_profile") or "default", target,
                "note_collab", nid,
                room.get("title") or nid,
                permissions=("read" if room.get("invited_rights") == "read" else "write"),
            )
            if share_res.get("ok"):
                index = share_module._load_share_index(target)
                for e in index.get("shared_with_me", []):
                    if e.get("share_id") == share_res["share_id"]:
                        e["collab_token"] = room["token"]
                        e["collab_note_id"] = nid
                        e["collab_access_mode"] = room.get("access_mode", "invite")
                share_module._save_share_index(target, index)
            _persist_room(nid, room)
            return {"ok": True, "accepted": True, "profile": target, "name": tname or req.get("name")}
        _persist_room(nid, room)
        return {"ok": True, "accepted": False}


# ─── Администрирование: модераторы, выгон, права, выход ───────────────


def _is_admin(room, pid):
    """Владелец или модератор комнаты."""
    if not pid:
        return False
    if room.get("owner_profile") and pid == room.get("owner_profile"):
        return True
    return pid in (room.get("moderators") or [])


def _room_for_action(nid, token):
    """Получить комнату по nid+token (с восстановлением из меты)."""
    room = ROOMS.get(nid)
    if not room:
        room = _ensure_room_from_meta(nid)
        if room:
            ROOMS[nid] = room
    if not room or room["token"] != token:
        return None
    return room


def set_member_rights(nid, token, target_profile_id, rights):
    """Владелец/модератор меняет права участника: 'write' | 'read'.

    Возвращает актуального участника комнаты (uid) для WS-уведомления.
    """
    with ROOMS_LOCK:
        room = _room_for_action(nid, token)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        if room.get("closed"):
            return {"ok": False, "error": "Совместный доступ закрыт", "code": "closed"}
        pid = _active_profile_id()
        if not _is_admin(room, pid):
            return {"ok": False, "error": "Только владелец или модератор может менять права"}
        if not target_profile_id:
            return {"ok": False, "error": "Целевой профиль не указан"}
        owner = room.get("owner_profile")
        if owner and target_profile_id == owner:
            return {"ok": False, "error": "Нельзя менять права владельца"}
        rights = "write" if rights == "write" else "read"
        room.setdefault("write_profiles", [])
        if rights == "write":
            if target_profile_id not in room["write_profiles"]:
                room["write_profiles"].append(target_profile_id)
            room.setdefault("allowed_profiles", [])
            if target_profile_id not in room["allowed_profiles"]:
                room["allowed_profiles"].append(target_profile_id)
        else:
            room["write_profiles"] = [
                p for p in room["write_profiles"] if p != target_profile_id]
        _persist_room(nid, room)
        target_uid = _uid_for_profile(room, target_profile_id)
        return {"ok": True, "rights": rights, "target_uid": target_uid}


def kick_member(nid, token, target_profile_id):
    """Водобный/администратор исключает участника из комнаты.

    Исключённый попадает в banned_profiles — даже с токеном вход закрыт.
    Возвращает target_uid для разрыва его WebSocket.
    """
    with ROOMS_LOCK:
        room = _room_for_action(nid, token)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        if room.get("closed"):
            return {"ok": False, "error": "Совместный доступ закрыт", "code": "closed"}
        pid = _active_profile_id()
        if not _is_admin(room, pid):
            return {"ok": False, "error": "Только владелец или модератор может выгнать"}
        if not target_profile_id:
            return {"ok": False, "error": "Целевой профиль не указан"}
        owner = room.get("owner_profile")
        if owner and target_profile_id == owner:
            return {"ok": False, "error": "Нельзя выгнать владельца комнаты"}
        room.setdefault("banned_profiles", [])
        if target_profile_id not in room["banned_profiles"]:
            room["banned_profiles"].append(target_profile_id)
        room["allowed_profiles"] = [p for p in (room.get("allowed_profiles") or []) if p != target_profile_id]
        room["write_profiles"] = [p for p in (room.get("write_profiles") or []) if p != target_profile_id]
        room["moderators"] = [p for p in (room.get("moderators") or []) if p != target_profile_id]
        _persist_room(nid, room)
        target_uid = _uid_for_profile(room, target_profile_id)
        return {"ok": True, "target_uid": target_uid}


def set_moderator(nid, token, target_profile_id, on):
    """Только владелец назначает/снимает модератора."""
    with ROOMS_LOCK:
        room = _room_for_action(nid, token)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        if room.get("closed"):
            return {"ok": False, "error": "Совместный доступ закрыт", "code": "closed"}
        pid = _active_profile_id()
        owner = room.get("owner_profile")
        if not pid or not owner or pid != owner:
            return {"ok": False, "error": "Только владелец может назначать модераторов"}
        if not target_profile_id or target_profile_id == owner:
            return {"ok": False, "error": "Некорректный целевой профиль"}
        room.setdefault("moderators", [])
        room.setdefault("allowed_profiles", [])
        room.setdefault("write_profiles", [])
        if on:
            if target_profile_id not in room["moderators"]:
                room["moderators"].append(target_profile_id)
            if target_profile_id not in room["allowed_profiles"]:
                room["allowed_profiles"].append(target_profile_id)
            if target_profile_id not in room["write_profiles"]:
                room["write_profiles"].append(target_profile_id)
        else:
            room["moderators"] = [p for p in room["moderators"] if p != target_profile_id]
        _persist_room(nid, room)
        return {"ok": True, "moderator": on}


def leave_room_profile(nid, token):
    """Участник покидает комнату (выход = отказ от доступа).

    Убирает себя из white-list и отзывает запись note_collab в своём инбоксе.
    Возвращает свой uid, чтобы клиент закрыл WebSocket.
    """
    with ROOMS_LOCK:
        pid = _active_profile_id()
        room = _room_for_action(nid, token)
        if not room:
            return {"ok": False, "error": "Комната не найдена"}
        if not pid:
            return {"ok": False, "error": "Профиль не определён"}
        if room.get("owner_profile") == pid:
            return {"ok": False, "error": "Владельцу нельзя покинуть комнату — закройте доступ"}
        room["allowed_profiles"] = [p for p in (room.get("allowed_profiles") or []) if p != pid]
        room["write_profiles"] = [p for p in (room.get("write_profiles") or []) if p != pid]
        room["moderators"] = [p for p in (room.get("moderators") or []) if p != pid]
        _persist_room(nid, room)
        my_uid = _uid_for_profile(room, pid)
    try:
        import share_module
        index = share_module._load_share_index(pid)
        before = len(index.get("shared_with_me", []))
        index["shared_with_me"] = [
            e for e in index.get("shared_with_me", [])
            if not (e.get("item_id") == nid and e.get("type") == "note_collab")
        ]
        if len(index.get("shared_with_me", [])) != before:
            share_module._save_share_index(pid, index)
    except Exception:
        swallow.note('collab_module.leave_room_profile:L1319')
        pass
    return {"ok": True, "uid": my_uid}


def _uid_for_profile(room, profile_id):
    if not profile_id:
        return None
    for u, v in (room.get("users") or {}).items():
        if v.get("profile_id") == profile_id:
            return u
    return None


def get_my_rooms(profile_id=None):
    """Активные комнаты, где профиль имеет доступ:
    владелец, модератор или приглашённый (allowed). Закрытые — исключаются."""
    try:
        if not profile_id:
            profile_id = _active_profile_id()
    except Exception:
        swallow.note('collab_module.get_my_rooms:L1342')
        pass
    if not profile_id:
        return []
    rooms = []
    seen = set()
    with ROOMS_LOCK:
        for nid, room in ROOMS.items():
            if room.get("closed"):
                continue
            role = _profile_role(room, profile_id)
            if not role:
                continue
            seen.add(nid)
            rooms.append({
                "nid": nid,
                "title": room.get("title") or nid,
                "token": room["token"],
                "role": role,
                "is_owner": room.get("owner_profile") == profile_id,
                "is_moderator": profile_id in (room.get("moderators") or []),
            })
    meta_dir = _COLLAB_DIR
    try:
        if os.path.isdir(meta_dir):
            for fn in sorted(os.listdir(meta_dir)):
                if not fn.endswith(".json"):
                    continue
                nid = fn[:-5]
                if nid in seen or not nid.isdigit():
                    continue
                meta = _load_meta(nid)
                if not meta or meta.get("closed"):
                    continue
                if meta.get("owner_profile") == profile_id or \
                        profile_id in (meta.get("allowed_profiles") or []) or \
                        profile_id in (meta.get("moderators") or []):
                    seen.add(nid)
                    rooms.append({
                        "nid": nid,
                        "title": meta.get("title") or nid,
                        "token": meta.get("token"),
                        "role": "write" if (
                            meta.get("owner_profile") == profile_id or
                            profile_id in (meta.get("write_profiles") or []) or
                            profile_id in (meta.get("moderators") or [])
                        ) else meta.get("invited_rights", "write"),
                        "is_owner": meta.get("owner_profile") == profile_id,
                        "is_moderator": profile_id in (meta.get("moderators") or []),
                    })
    except Exception:
        swallow.note('collab_module.get_my_rooms:L1393')
        pass
    rooms.sort(key=lambda r: r["title"].lower())
    return rooms


# ─── Удалённые комнаты (кросс-серверная координация) ───────────────────

def _remote_node_id():
    try:
        import coordination_module
        return coordination_module.node_id()
    except Exception:
        swallow.note('collab_module._remote_node_id:L1406')
        return ""


def _remote_enabled():
    try:
        import coordination_module
        return coordination_module.enabled()
    except Exception:
        swallow.note('collab_module._remote_enabled:L1415')
        return False


def _ensure_remote_subscriptions():
    """Подписать обработчики каналов координации (идентично повторному вызову)."""
    global _REMOTE_SUBS_READY
    try:
        import coordination_module
        coordination_module.subscribe("collab-invite", _relay_dispatch)
        coordination_module.subscribe("collab:", _relay_dispatch)
        coordination_module.subscribe_channel("collab-invite")
        for nid in _remote_room_ids():
            coordination_module.subscribe_channel("collab:" + nid)
        _REMOTE_SUBS_READY = True
    except Exception:
        swallow.note('collab_module._ensure_remote_subscriptions:L1431')
        pass


def _remote_room_ids():
    """ID комнат, участвующих в удалённой коллаборации (для подписок после рестарта)."""
    ids = []
    try:
        with ROOMS_LOCK:
            for nid, room in ROOMS.items():
                if room.get("remote_mode"):
                    ids.append(nid)
    except Exception:
        swallow.note('collab_module._remote_room_ids:L1444')
        pass
    try:
        import share_module
        import profiles_module
        active = profiles_module.get_active_profile()
        pid = active["id"] if active else "default"
        index = share_module._load_share_index(pid)
        for e in index.get("shared_with_me", []):
            if e.get("type") == "note_collab" and e.get("collab_remote"):
                ids.append(e.get("collab_note_id") or e.get("item_id"))
    except Exception:
        swallow.note('collab_module._remote_room_ids:L1456')
        pass
    return ids


def _relay_dispatch(channel, payload, from_node):
    """Диспетчер событий каналов координации для коллаборации."""
    try:
        if not _REMOTE_SUBS_READY:
            _ensure_remote_subscriptions()
        if from_node and from_node == _remote_node_id():
            return
        if channel == "collab-invite":
            handle_remote_invite(payload)
            return
        if channel.startswith("collab:"):
            nid = channel[len("collab:"):]
            with _REMOTE_LOCK:
                mirror = _REMOTE_ROOMS.get(nid)
            if mirror:
                loop = mirror.get("loop")
                q = mirror.get("queue")
                if loop and q:
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, payload)
                    except Exception:
                        swallow.note('collab_module._relay_dispatch:L1482')
                        pass
            else:
                handle_remote_room_event(nid, payload, from_node)
    except Exception as e:
        logger.error(f"collab: relay dispatch: {e}")


def invite_remote(nid, target_node_id, title=""):
    """Владелец приглашает ноду из внешней сети в комнату.

    Отправляет приглашение на ноду (канал collab-invite) и включает
    публикацию событий комнаты на канал collab:<nid>.
    """
    try:
        pid = _active_profile_id()
        with ROOMS_LOCK:
            room = ROOMS.get(nid)
            if not room:
                room = _ensure_room_from_meta(nid)
                if room:
                    ROOMS[nid] = room
            if not room:
                return {"ok": False, "error": "Комната не найдена"}
            if room.get("owner_profile") and room.get("owner_profile") != pid:
                return {"ok": False, "error": "Только владелец может приглашать"}
            if room.get("closed"):
                return {"ok": False, "error": "Совместный доступ закрыт", "code": "closed"}
        import coordination_module
        if not coordination_module.enabled():
            return {"ok": False, "error": "Координация выключена — включите её в настройках сети"}
        peers = {p["node_id"]: p for p in coordination_module.get_peers()}
        peer = peers.get(target_node_id)
        if not peer:
            return {"ok": False, "error": "Нода не найдена в сети"}
        with ROOMS_LOCK:
            room = ROOMS.get(nid)
            if room:
                room["remote_mode"] = True
        _ensure_remote_subscriptions()
        coordination_module.subscribe_channel("collab:" + nid)
        ok = coordination_module.send_event(target_node_id, "collab-invite", {
            "nid": nid,
            "token": room["token"],
            "title": room.get("title") or nid,
            "owner_name": room.get("owner") or "Владелец",
            "owner_node": coordination_module.node_id(),
            "access_mode": room.get("access_mode", "invite"),
            "invited_rights": room.get("invited_rights", "write"),
        })
        if ok:
            with ROOMS_LOCK:
                room = ROOMS.get(nid)
                if room:
                    room["remote_mode"] = True
                    _persist_room(nid, room)
            return {"ok": True, "message": "Приглашение отправлено ноде " + peer.get("name", "")}
        return {"ok": False, "error": "Не удалось доставить приглашение"}
    except Exception as e:
        swallow.note('collab_module.invite_remote:L1541', e)
        return {"ok": False, "error": str(e)}


def handle_remote_invite(payload):
    """Принять приглашение в комнату другой ноды: запись в share_index."""
    try:
        nid = str(payload.get("nid") or "")
        node = str(payload.get("owner_node") or "")
        token = str(payload.get("token") or "")
        title = str(payload.get("title") or nid)
        if not nid or not node or not token:
            return
        import share_module
        import profiles_module
        active = profiles_module.get_active_profile()
        pid = active["id"] if active else "default"
        share_id = str(uuid.uuid4())[:8]
        index = share_module._load_share_index(pid)
        existing = [e for e in index.get("shared_with_me", [])
                    if e.get("type") == "note_collab"
                    and e.get("collab_remote")
                    and e.get("collab_remote", {}).get("node") == node
                    and (e.get("collab_note_id") or e.get("item_id")) == nid]
        if not existing:
            entry = {
                "share_id": share_id,
                "type": "note_collab",
                "item_id": nid,
                "title": title,
                "source_profile": "coordination",
                "target_profile": pid,
                "permissions": "write",
                "shared_at": time.time(),
                "collab_token": token,
                "collab_note_id": nid,
                "collab_remote": {
                    "node": node,
                    "nid": nid,
                    "token": token,
                    "owner_name": payload.get("owner_name") or "",
                    "title": title,
                    "access_mode": payload.get("access_mode", "invite"),
                    "invited_rights": payload.get("invited_rights", "write"),
                },
            }
            index.setdefault("shared_with_me", []).append(entry)
            share_module._save_share_index(pid, index)
        _ensure_remote_subscriptions()
        import coordination_module
        coordination_module.subscribe_channel("collab:" + nid)
        logger.info(f"collab: получено приглашение в комнату {nid} от ноды {node}")
    except Exception as e:
        logger.error(f"collab: remote invite: {e}")


def _ensure_remote_user(room, uid, name):
    if uid not in room["users"]:
        color = PALETTE[len([u for u in room["users"].values() if not u.get("_retired")]) % len(PALETTE)]
        room["users"][uid] = {"name": name, "color": color, "online": True,
                              "profile_id": None, "_remote": True}
    else:
        room["users"][uid]["online"] = True
        room["users"][uid]["name"] = name


def handle_remote_room_event(nid, payload, from_node):
    """Событие от удалённой ноды для комнаты, где мы владелец."""
    try:
        with ROOMS_LOCK:
            room = ROOMS.get(nid)
            if not room:
                room = _ensure_room_from_meta(nid)
                if room:
                    ROOMS[nid] = room
            if not room or room.get("closed"):
                return
            room["remote_mode"] = True
            uid = "remote:" + (from_node or "?")[:10]
            mtype = payload.get("type")
            if mtype == "join":
                _ensure_remote_user(room, uid, payload.get("name") or "Гость")
                _persist_room(nid, room)
                _broadcast_local(nid, _presence_payload(room))
                return
            if mtype == "leave":
                u = room["users"].get(uid)
                if u:
                    u["online"] = False
                _persist_room(nid, room)
                _broadcast_local(nid, _presence_payload(room))
                return
            if mtype in ("update", "chat"):
                _ensure_remote_user(room, uid, payload.get("name") or "Гость")
        if mtype == "update":
            event = apply_update(nid, uid, payload.get("text", ""), payload.get("title"))
            if event:
                _broadcast_local(nid, event)
            return
        if mtype == "chat":
            chat_msg = send_chat(nid, uid, payload.get("text", ""))
            if chat_msg:
                _broadcast_local(nid, {"type": "chat", **chat_msg})
            return
        if mtype == "rtc":
            with ROOMS_LOCK:
                room = ROOMS.get(nid)
                if room:
                    clients = list(room.get("clients", {}).items())
                else:
                    clients = []
            for cuid, q in clients:
                try:
                    q.put_nowait({"type": "rtc", "from": uid,
                                  "data": payload.get("data", {})})
                except Exception:
                    swallow.note('collab_module.handle_remote_room_event:L1657')
                    pass
            return
    except Exception as e:
        logger.error(f"collab: remote room event: {e}")


def remote_join_room(nid, node, token, snapshot, uid, queue_obj, loop):
    """Создать/получить зеркало удалённой комнаты и подключить локального клиента."""
    with _REMOTE_LOCK:
        mirror = _REMOTE_ROOMS.get(nid)
        if not mirror:
            mirror = {
                "nid": nid,
                "node": node,
                "token": token,
                "queue": asyncio.Queue(),
                "loop": loop,
                "title": snapshot.get("title", ""),
                "content": snapshot.get("content", ""),
                "segments": snapshot.get("segments") or [],
                "line_owners": snapshot.get("line_owners") or [],
                "messages": snapshot.get("messages") or [],
                "users": snapshot.get("users") or {},
                "seq": snapshot.get("seq", 0),
                "closed": bool(snapshot.get("closed")),
                "access_mode": snapshot.get("access_mode", "invite"),
                "invited_rights": snapshot.get("invited_rights", "write"),
                "owner": snapshot.get("owner", ""),
                "clients": {},
            }
            _REMOTE_ROOMS[nid] = mirror
        mirror["clients"][uid] = queue_obj
        mirror["loop"] = loop
        return True


def remote_room(nid):
    with _REMOTE_LOCK:
        return _REMOTE_ROOMS.get(nid)


def remote_client_leave(nid, uid):
    with _REMOTE_LOCK:
        mirror = _REMOTE_ROOMS.get(nid)
        if not mirror:
            return
        mirror["clients"].pop(uid, None)
        if not mirror["clients"]:
            _REMOTE_ROOMS.pop(nid, None)


def remote_apply_event(nid, payload):
    """Применить событие от владельца к зеркалу удалённой комнаты."""
    with _REMOTE_LOCK:
        mirror = _REMOTE_ROOMS.get(nid)
        if not mirror:
            return None
        mtype = payload.get("type")
        if mtype == "broadcast":
            mirror["content"] = payload.get("text", mirror["content"])
            if "title" in payload:
                mirror["title"] = payload["title"]
            mirror["segments"] = payload.get("segments") or mirror["segments"]
            mirror["line_owners"] = payload.get("line_owners") or mirror["line_owners"]
            mirror["seq"] = payload.get("seq", mirror["seq"])
        elif mtype == "chat":
            mirror.setdefault("messages", []).append({
                "ts": payload.get("ts", time.time()),
                "uid": payload.get("uid", ""),
                "name": payload.get("name", ""),
                "text": payload.get("text", ""),
            })
            mirror["messages"] = mirror["messages"][-200:]
        elif mtype == "presence":
            mirror["users"] = payload.get("users") or mirror["users"]
        return payload


def get_remote_snapshot(nid, token):
    """Снапшот комнаты для удалённой ноды (проверяется токен)."""
    with ROOMS_LOCK:
        room = ROOMS.get(nid)
        if not room:
            room = _ensure_room_from_meta(nid)
            if room:
                ROOMS[nid] = room
        if not room:
            return {"active": False}
        if room["token"] != token:
            return {"active": False, "error": "Неверный токен"}
        return {
            "active": True,
            "owner": room["owner"],
            "title": room["title"],
            "content": room["content"],
            "segments": room["segments"],
            "line_owners": room["line_owners"],
            "messages": room["messages"],
            "users": {u: {**v, "online": bool(v.get("online"))}
                      for u, v in room["users"].items()},
            "seq": room["seq"],
            "access_mode": room.get("access_mode"),
            "invited_rights": room.get("invited_rights"),
            "closed": bool(room.get("closed")),
            "token": room["token"],
        }


# Публикация событий комнаты на канал координации (фоновый поток)

def _start_pub_thread():
    global _PUB_THREAD
    with _PUB_THREAD_LOCK:
        if _PUB_THREAD and _PUB_THREAD.is_alive():
            return
        def _worker():
            while True:
                item = _PUB_QUEUE.get()
                if item is None:
                    break
                try:
                    nid, event = item
                    _do_publish(nid, event)
                except Exception:
                    swallow.note('collab_module._worker:L1782')
                    pass
        _PUB_THREAD = threading.Thread(target=_worker, daemon=True,
                                       name="collab-pub")
        _PUB_THREAD.start()


def _do_publish(nid, event):
    try:
        import coordination_module
        if not coordination_module.enabled():
            return
        room = ROOMS.get(nid)
        if not room or not room.get("remote_mode"):
            return
        evt = {"type": event["type"]}
        if event["type"] == "broadcast":
            for k in ("seq", "text", "title", "segments", "line_owners", "from"):
                if k in event:
                    evt[k] = event[k]
        elif event["type"] == "chat":
            for k in ("ts", "uid", "name", "text"):
                if k in event:
                    evt[k] = event[k]
        else:
            evt = event
        coordination_module.send_broadcast("collab:" + nid, evt)
    except Exception as e:
        logger.error(f"collab: publish: {e}")


def _publish_room_event(nid, event):
    try:
        import coordination_module
        if not coordination_module.enabled():
            return
        room = ROOMS.get(nid)
        if not room or not room.get("remote_mode"):
            return
        _start_pub_thread()
        _PUB_QUEUE.put((nid, event))
    except Exception:
        swallow.note('collab_module._publish_room_event:L1824')
        pass
