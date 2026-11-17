"""Сессии и папки чата.

Перенесено из server_backup.py без изменения тел. Пути (`CHAT_DIR`,
`CHAT_FOLDERS_FILE`), кэш сессий и блокировка читаются через _sb в момент
вызова: apply_profile_globals переписывает их в globals() монолита при смене
профиля, поэтому копирование по значению заморозило бы путь первого профиля.
"""

import json
import os
import time

import server_backup as _sb
import profile_paths
import swallow
from modules import public_view as _pv

_pp = profile_paths.proxy(_sb)   # каталоги профиля запроса; вне запроса — глобалы _sb


def _cache_key(session_id):
    """Ключ кэша сессий: профиль + id.

    CHAT_SESSIONS — один dict в globals() монолита на весь процесс, а его
    ключом был голый id. Просьба «session?id=<чужой id>» попадала прямо в
    переписку другого профиля, минуя каталог профиля: файл-то искали у себя,
    а кэш — нет. Записи, положенные в тот же dict по старому ключу другими
    маршрутами, с кортежным ключом просто не читаются.
    """
    return (profile_paths.current_profile_id() or "", session_id)


def _cache_get(session_id):
    with _sb._chat_lock:
        return _sb.CHAT_SESSIONS.get(_cache_key(session_id))


def _cache_put(session_id, session):
    with _sb._chat_lock:
        _sb.CHAT_SESSIONS[_cache_key(session_id)] = session


def _cache_pop(session_id):
    with _sb._chat_lock:
        _sb.CHAT_SESSIONS.pop(_cache_key(session_id), None)


def _own_file(session_id):
    """Файл сессии в КАТАЛОГЕ ПРОФИЛЯ запроса.

    Запись через public_view.read_file нашла бы файл общедоступного профиля и
    правка чужой сессии ушла бы в её оригинал; copy-on-write, который обещает
    chat_list_sessions, получается только если писать всегда к себе.
    """
    d = _pp.CHAT_DIR
    if not d:
        return ""
    return os.path.join(d, f"{session_id}.json")


def _taken_by_other_profile(session_id):
    """Занят ли id файлом в чужом каталоге чата (и не показан мне общедоступным)."""
    pid = profile_paths.current_profile_id()
    name = "%s.json" % session_id
    try:
        import profiles_module as PM
        others = [p.get("id") for p in PM._load_index().get("profiles", [])
                  if p.get("id") and p.get("id") != pid]
    except Exception:
        swallow.note('chat_store._taken_by_other_profile:L66')
        return False
    for other in others:
        try:
            fp = os.path.join(profile_paths.dir_for(other, "CHAT_DIR"), name)
        except Exception:
            swallow.note('chat_store._taken_by_other_profile:L73')
            continue
        if os.path.isfile(fp):
            return True
    return False


def chat_get_folders():
    """Получить список папок чатов."""
    default_folders = [{"id": "all", "name": "Все чаты", "icon": "💬"}, {"id": "inbox", "name": "Входящие", "icon": "📥"}]
    try:
        if os.path.exists(_sb.CHAT_FOLDERS_FILE):
            with open(_sb.CHAT_FOLDERS_FILE, encoding="utf-8") as f:
                custom = json.load(f)
            # Фильтруем дубликаты системных папок из файла
            seen = set()
            result = []
            for f in default_folders + custom:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    result.append(f)
            return result
    except Exception:
        swallow.note('chat_store.chat_get_folders:L35')
        pass
    return default_folders

def chat_save_folders(folders):
    """Сохранить папки чатов."""
    try:
        fp = _sb.CHAT_FOLDERS_FILE
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(folders, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        swallow.note('chat_store.chat_save_folders:L48')
        return False

def chat_create_folder(name, icon="📁", parent_id=None):
    """Создать папку для чатов."""
    folders = chat_get_folders()
    existing = [f["id"] for f in folders if f["id"] not in ("all", "inbox")]
    folder_id = f"folder_{int(time.time()*1000)}"
    folder_data = {"id": folder_id, "name": name, "icon": icon}
    if parent_id:
        folder_data["parent_id"] = parent_id
    folders.append(folder_data)
    if not chat_save_folders(folders):
        return {"error": "Не удалось сохранить папки"}
    return {"ok": True, "id": folder_id}

def chat_rename_folder(folder_id, new_name, parent_id=None):
    """Переименовать папку и/или переместить (изменить parent_id)."""
    folders = chat_get_folders()
    for f in folders:
        if f["id"] == folder_id and f["id"] not in ("all", "inbox"):
            f["name"] = new_name
            if parent_id is not None:
                if parent_id == "" or parent_id is None:
                    f.pop("parent_id", None)
                else:
                    f["parent_id"] = parent_id
            chat_save_folders(folders)
            return {"ok": True}
    return {"error": "Папка не найдена"}

def chat_delete_folder(folder_id):
    """Удалить папку (чаты не удаляются, перемещаются в inbox)."""
    if folder_id in ("all", "inbox"):
        return {"error": "Нельзя удалить системную папку"}
    folders = chat_get_folders()
    folders = [f for f in folders if f["id"] != folder_id]
    chat_save_folders(folders)
    # Перемещаем чаты из удалённой папки в inbox
    try:
        for fn in os.listdir(_pp.CHAT_DIR):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            fp = os.path.join(_pp.CHAT_DIR, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    s = json.load(f)
                if s.get("folder") == folder_id:
                    s["folder"] = "inbox"
                    with open(fp, "w", encoding="utf-8") as f:
                        json.dump(s, f, ensure_ascii=False)
            except Exception:
                swallow.note('chat_store.chat_delete_folder:L100')
                continue
    except Exception:
        swallow.note('chat_store.chat_delete_folder:L103')
        pass
    return {"ok": True}

def chat_get_session(session_id=None):
    """Получить или создать сессию чата."""
    session_id = _sb._safe_session_id(session_id)
    if session_id:
        hit = _cache_get(session_id)
        if hit is not None:
            return hit
    # Пробуем загрузить из файла
    if session_id:
        fp = _pv.read_file("chat", f"{session_id}.json")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    session = json.load(f)
                _cache_put(session_id, session)
                return session
            except Exception as e:
                print(f"Error loading chat session {session_id}: {e}")
                pass
        else:
            print(f"Chat file not found: {fp}")
    # Создаем новую сессию. Чужой id не присваиваем: иначе правка по
    # подсмотренному id оставила бы копию чужого чата в моём каталоге.
    sid = session_id or ""
    if sid and _taken_by_other_profile(sid):
        sid = ""
    sid = sid or f"chat_{int(time.time()*1000)}"
    session = {"id": sid, "name": "", "folder": "inbox", "messages": [], "created": time.time(), "updated": time.time()}
    _cache_put(sid, session)
    return session

def chat_add_message(session_id, role, content):
    """Добавить сообщение в сессию."""
    with _sb._chat_lock:
        session = chat_get_session(session_id)
        session["messages"].append({"role": role, "content": content, "time": time.time()})
        session["updated"] = time.time()
        if len(session["messages"]) > 100:
            session["messages"] = session["messages"][-100:]
        session_copy = dict(session)
    try:
        os.makedirs(_pp.CHAT_DIR, exist_ok=True)
        fp = _own_file(session_copy['id'])
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(session_copy, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"chat_add_message error: {e}")
        pass
    return session

def chat_update_message(session_id, msg_index, new_content):
    """Обновить сообщение в сессии."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msgs[msg_index]["content"] = new_content
        session["updated"] = time.time()
        try:
            fp = _own_file(session['id'])
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False)
        except Exception:
            swallow.note('chat_store.chat_update_message:L166')
            pass
        return True
    return False

def chat_delete_message(session_id, msg_index):
    """Удалить сообщение из сессии."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msgs.pop(msg_index)
        session["updated"] = time.time()
        try:
            fp = _own_file(session['id'])
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False)
        except Exception:
            swallow.note('chat_store.chat_delete_message:L183')
            pass
        return True
    return False

def chat_toggle_favorite(session_id, msg_index):
    """Переключить избранное сообщения."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msgs[msg_index]["favorite"] = not msgs[msg_index].get("favorite", False)
        session["updated"] = time.time()
        try:
            fp = _own_file(session['id'])
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False)
        except Exception:
            swallow.note('chat_store.chat_toggle_favorite:L200')
            pass
        return msgs[msg_index]["favorite"]
    return False

def chat_get_favorites(session_id):
    """Получить избранные сообщения."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    return [m for m in msgs if m.get("favorite", False)]

def chat_move_to_notes(session_id, msg_index):
    """Перенести сообщение в заметки."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if 0 <= msg_index < len(msgs):
        msg = msgs[msg_index]
        content = f"[Из чата]\n\n{msg.get('content', '')}"
        try:
            note_id = f"note_{int(time.time()*1000)}"
            folder_dir = os.path.join(_pp.NOTES_DIR, "chat_favorites")
            os.makedirs(folder_dir, exist_ok=True)
            fp = os.path.join(folder_dir, f"{note_id}.json")
            article = {"id": note_id, "title": "Из чата", "content": content, "folder": "chat_favorites", "created": time.time(), "updated": time.time()}
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(article, f, ensure_ascii=False, indent=2)
            return note_id
        except Exception as e:
            print(f"Error saving note: {e}")
            return None
    return None

def chat_get_messages(session_id, max_messages=10):
    """Получить историю сообщений для контекста."""
    session = chat_get_session(session_id)
    msgs = session.get("messages", [])
    if max_messages == 0:
        return msgs
    return msgs[-max_messages:] if msgs else []

def chat_list_sessions(folder=None, source=None):
    """Список всех сессий чата."""
    sessions = []
    seen = set()
    for d in _pv.read_dirs("chat"):
        try:
            for f in os.listdir(d):
                if not f.endswith(".json") or f.startswith("_"):
                    continue
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        s = json.load(fh)
                    if folder and folder != "all":
                        if s.get("folder", "inbox") != folder:
                            continue
                    if source:
                        if s.get("source") != source:
                            continue
                    sid = s.get("id", "")
                    # Своё всегда выигрывает у общего: после правки общедоступной
                    # сессии её копия лежит в своём каталоге, и в списке она одна.
                    if sid in seen:
                        continue
                    seen.add(sid)
                    sessions.append({
                        "id": sid,
                        "name": s.get("name", "") or (s.get("messages", [{}])[0].get("content", "")[:60] if s.get("messages") else ""),
                        "folder": s.get("folder", "inbox"),
                        "messages_count": len(s.get("messages", [])),
                        "created": s.get("created", 0),
                        "updated": s.get("updated", 0),
                        "preview": s.get("messages", [{}])[-1].get("content", "")[:100] if s.get("messages") else ""
                    })
                except Exception:
                    swallow.note('chat_store.chat_list_sessions:L274')
                    continue
        except Exception:
            swallow.note('chat_store.chat_list_sessions:L277')
            pass
    sessions.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return sessions

def chat_delete_session(session_id):
    """Удалить сессию чата — только свою копию.

    read_file нашёл бы файл общедоступного профиля, и «удалил чат» стал бы
    удалением чужих данных; с copy-on-write своя копия живёт в CHAT_DIR.
    """
    try:
        session_id = _sb._safe_session_id(session_id)
        if not session_id:
            return False
        fp = _own_file(session_id)
        if fp and os.path.exists(fp):
            os.remove(fp)
        _cache_pop(session_id)
        return True
    except Exception:
        swallow.note('chat_store.chat_delete_session:L296')
        return False

def chat_rename_session(session_id, new_name):
    """Переименовать чат."""
    session = chat_get_session(session_id)
    session["name"] = new_name
    session["updated"] = time.time()
    try:
        fp = _own_file(session['id'])
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False)
        return {"ok": True}
    except Exception:
        swallow.note('chat_store.chat_rename_session:L310')
        return {"error": "Не удалось переименовать"}

def chat_move_session(session_id, folder_id):
    """Переместить чат в папку."""
    session = chat_get_session(session_id)
    session["folder"] = folder_id
    session["updated"] = time.time()
    try:
        fp = _own_file(session['id'])
        os.makedirs(_pp.CHAT_DIR, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False)
        return {"ok": True}
    except Exception:
        swallow.note('chat_store.chat_move_session:L325')
        return {"error": "Не удалось переместить"}
