#!/usr/bin/env python3
"""
Share Module — система шеринга между профилями Infinity AI Search System.

Два механизма:
A) Публикация в Общедоступный — копирование контента в data/profiles/default/data/<type>/shared/
B) Кросспрофильные ссылки — ссылка на элемент в другом профиле без копирования
"""

import os
import json
import time
import uuid
import shutil
import logging
import swallow

logger = logging.getLogger("infinity")

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_APP_DIR)


def _data_dir():
    """Корневой data-каталог приложения.

    Раньше здесь было os.path.dirname(__file__)/data, т.е. app/data.
    Но реальные профили (и их заметки) лежат в <корень>/data/profiles,
    поэтому шеринг писал share_index и копии заметок в «пустую» ветку
    app/data/profiles: получатель видел запись, а файла не было.
    """
    env = os.environ.get("INFINITY_DATA_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data")
    except Exception:
        swallow.note('share_module._data_dir:L38')
        pass
    if os.path.isdir(os.path.join(_PARENT_DIR, "data")):
        return os.path.join(_PARENT_DIR, "data")
    return os.path.join(_APP_DIR, "data")


DATA_DIR = _data_dir()
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")

# Типы контента, доступные для шеринга
PUBLISHABLE_TYPES = {
    "note": {"dir": "notes", "ext": ".json", "single_file": True},
    "chat": {"dir": "chat_sessions", "ext": ".json", "single_file": True},
    "memory": {"dir": "memory", "ext": ".json", "single_file": True},
    "ai_answer": {"dir": "ai_answers", "ext": ".json", "single_file": True},
    "attachment": {"dir": "attachments", "ext": None, "single_file": True},
    "plugin": {"dir": "plugins", "ext": None, "single_file": True},
    "app": {"dir": "apps", "ext": None, "single_file": False},
    "html": {"dir": "html", "ext": None, "single_file": False},
    "extension": {"dir": "extensions", "ext": None, "single_file": False},
}

# Типы, которые шерим по ссылке (не копируем файлы)
LINK_ONLY_TYPES = {"music", "video", "photo", "zim", "book", "collection_book"}


def _profile_data_dir(profile_id):
    """Вернуть data-директорию профиля."""
    return os.path.join(PROFILES_DIR, profile_id, "data")


def _share_index_path(profile_id):
    """Путь к файлу индекса шеринга профиля."""
    return os.path.join(_profile_data_dir(profile_id), "share_index.json")


def _load_share_index(profile_id):
    """Загрузить share_index профиля."""
    path = _share_index_path(profile_id)
    if not os.path.exists(path):
        return {"published": [], "shared_with_me": [], "my_links": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        swallow.note('share_module._load_share_index:L84')
        return {"published": [], "shared_with_me": [], "my_links": []}


def _save_share_index(profile_id, index):
    """Сохранить share_index профиля."""
    path = _share_index_path(profile_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def _get_default_profile_id():
    """Получить ID общедоступного профиля."""
    index_path = os.path.join(PROFILES_DIR, "profiles_index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        for p in index.get("profiles", []):
            if p.get("name") == "Общедоступный":
                return p["id"]
        profiles = index.get("profiles", [])
        return profiles[0]["id"] if profiles else None
    except Exception:
        swallow.note('share_module._get_default_profile_id:L108')
        return None


def _get_active_profile_id():
    """Получить ID активного профиля."""
    index_path = os.path.join(PROFILES_DIR, "profiles_index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        return index.get("active")
    except Exception:
        swallow.note('share_module._get_active_profile_id:L120')
        return None


# ─── A: Публикация в Общедоступный ─────────────────────────────────────


def publish_item(source_profile_id, item_type, item_id, title=""):
    """Опубликовать элемент из профиля в общедоступный.

    Args:
        source_profile_id: ID профиля-источника
        item_type: тип элемента (note, chat, memory, ai_answer, app, html, ...)
        item_id: ID элемента (имя файла без расширения для single_file)
        title: заголовок для отображения

    Returns:
        dict с результатом {ok, share_id, error?}
    """
    default_id = _get_default_profile_id()
    if not default_id:
        return {"ok": False, "error": "Нет общедоступного профиля"}

    if source_profile_id == default_id:
        return {"ok": False, "error": "Нельзя публиковать из общедоступного профиля"}

    # Определяем тип публикации
    if item_type in PUBLISHABLE_TYPES:
        return _publish_file_item(source_profile_id, default_id, item_type, item_id, title)
    elif item_type in LINK_ONLY_TYPES:
        return _publish_link_item(source_profile_id, default_id, item_type, item_id, title)
    else:
        return {"ok": False, "error": f"Неизвестный тип: {item_type}"}


def _publish_file_item(source_id, target_id, item_type, item_id, title):
    """Копировать файл из профиля в shared-директорию общедоступного."""
    type_info = PUBLISHABLE_TYPES[item_type]
    subdir = type_info["dir"]
    ext = type_info["ext"]

    source_base = _profile_data_dir(source_id)
    target_base = _profile_data_dir(target_id)

    source_dir = os.path.join(source_base, subdir)
    target_dir = os.path.join(target_base, subdir, "shared")
    os.makedirs(target_dir, exist_ok=True)

    if type_info["single_file"]:
        filename = item_id + (ext if ext else "")
        src_path = os.path.join(source_dir, filename)
        if not os.path.exists(src_path):
            return {"ok": False, "error": f"Файл не найден: {filename}"}
        dst_path = os.path.join(target_dir, filename)
        shutil.copy2(src_path, dst_path)
    else:
        # Папка (app, html, extension)
        src_path = os.path.join(source_dir, item_id)
        if not os.path.isdir(src_path):
            return {"ok": False, "error": f"Папка не найдена: {item_id}"}
        dst_path = os.path.join(target_dir, item_id)
        if os.path.exists(dst_path):
            shutil.rmtree(dst_path)
        shutil.copytree(src_path, dst_path)

    # Записываем в share_index
    share_id = str(uuid.uuid4())[:8]
    now = time.time()
    entry = {
        "share_id": share_id,
        "type": item_type,
        "item_id": item_id,
        "title": title or item_id,
        "source_profile": source_id,
        "published_at": now,
    }

    index = _load_share_index(source_id)
    index.setdefault("published", []).append(entry)
    _save_share_index(source_id, index)

    # Добавляем получателю
    target_index = _load_share_index(target_id)
    target_index.setdefault("shared_with_me", []).append({
        **entry,
        "shared_at": now,
    })
    _save_share_index(target_id, target_index)

    return {"ok": True, "share_id": share_id, "path": dst_path}


def _publish_link_item(source_id, target_id, item_type, item_id, title):
    """Опубликовать ссылку на элемент (музыка, видео, фото, zim, книга)."""
    share_id = str(uuid.uuid4())[:8]
    now = time.time()
    entry = {
        "share_id": share_id,
        "type": item_type,
        "item_id": item_id,
        "title": title or item_id,
        "source_profile": source_id,
        "published_at": now,
    }

    index = _load_share_index(source_id)
    index.setdefault("published", []).append(entry)
    _save_share_index(source_id, index)

    target_index = _load_share_index(target_id)
    target_index.setdefault("shared_with_me", []).append({
        **entry,
        "shared_at": now,
    })
    _save_share_index(target_id, target_index)

    return {"ok": True, "share_id": share_id}


def unpublish_item(source_profile_id, share_id):
    """Отменить публикацию."""
    index = _load_share_index(source_profile_id)
    published = index.get("published", [])
    entry = next((e for e in published if e["share_id"] == share_id), None)
    if not entry:
        return {"ok": False, "error": "Публикация не найдена"}

    index["published"] = [e for e in published if e["share_id"] != share_id]
    _save_share_index(source_profile_id, index)

    default_id = _get_default_profile_id()
    if default_id:
        target_index = _load_share_index(default_id)
        target_index["shared_with_me"] = [
            e for e in target_index.get("shared_with_me", [])
            if e.get("share_id") != share_id
        ]
        _save_share_index(default_id, target_index)

    # Удаляем файл если был скопирован
    if entry.get("type") in PUBLISHABLE_TYPES:
        type_info = PUBLISHABLE_TYPES[entry["type"]]
        target_dir = os.path.join(
            _profile_data_dir(default_id), type_info["dir"], "shared"
        )
        if type_info["single_file"]:
            fpath = os.path.join(target_dir, entry["item_id"] + (type_info["ext"] or ""))
            if os.path.exists(fpath):
                os.remove(fpath)
        else:
            dpath = os.path.join(target_dir, entry["item_id"])
            if os.path.exists(dpath):
                shutil.rmtree(dpath)

    return {"ok": True}


def get_my_publications(profile_id):
    """Вернуть список публикаций профиля."""
    index = _load_share_index(profile_id)
    return index.get("published", [])


# ─── B: Кросспрофильные ссылки ─────────────────────────────────────


def share_with_profile(source_profile_id, target_profile_id, item_type, item_id, title="", permissions="read"):
    """Расшарить элемент конкретному профилю (без копирования).

    Args:
        source_profile_id: ID профиля-владельца
        target_profile_id: ID профиля-получателя
        item_type: тип элемента
        item_id: ID элемента
        title: заголовок
        permissions: права доступа ('read' | 'write')

    Returns:
        dict {ok, share_id, error?}
    """
    if source_profile_id == target_profile_id:
        return {"ok": False, "error": "Нельзя расшарить самому себе"}

    share_id = str(uuid.uuid4())[:8]
    now = time.time()
    entry = {
        "share_id": share_id,
        "type": item_type,
        "item_id": item_id,
        "title": title or item_id,
        "source_profile": source_profile_id,
        "target_profile": target_profile_id,
        "permissions": permissions,
        "shared_at": now,
    }

    # Записываем отправителю
    source_index = _load_share_index(source_profile_id)
    source_index.setdefault("my_links", []).append(entry)
    _save_share_index(source_profile_id, source_index)

    # Записываем получателю
    target_index = _load_share_index(target_profile_id)
    target_index.setdefault("shared_with_me", []).append(entry)
    _save_share_index(target_profile_id, target_index)

    return {"ok": True, "share_id": share_id}


def revoke_share(owner_profile_id, share_id):
    """Отозвать кросспрофильную ссылку."""
    index = _load_share_index(owner_profile_id)
    links = index.get("my_links", [])
    entry = next((e for e in links if e["share_id"] == share_id), None)
    if not entry:
        return {"ok": False, "error": "Ссылка не найдена"}

    index["my_links"] = [e for e in links if e["share_id"] != share_id]
    _save_share_index(owner_profile_id, index)

    target_id = entry.get("target_profile")
    if target_id:
        target_index = _load_share_index(target_id)
        target_index["shared_with_me"] = [
            e for e in target_index.get("shared_with_me", [])
            if e.get("share_id") != share_id
        ]
        _save_share_index(target_id, target_index)

    return {"ok": True}


def get_my_links(profile_id):
    """Вернуть исходящие кросспрофильные ссылки."""
    index = _load_share_index(profile_id)
    return index.get("my_links", [])


def get_incoming_shares(profile_id):
    """Вернуть входящие расшаренные элементы (публикации + ссылки)."""
    index = _load_share_index(profile_id)
    return index.get("shared_with_me", [])


# ─── C: Разрешение доступа ─────────────────────────────────────


def resolve_share(profile_id, share_id):
    """Получить контент расшаренного элемента (с проверкой прав).

    Args:
        profile_id: ID профиля, запрашивающего доступ
        share_id: ID шеринга

    Returns:
        dict с контентом или ошибкой
    """
    index = _load_share_index(profile_id)
    shared = index.get("shared_with_me", [])
    entry = next((e for e in shared if e["share_id"] == share_id), None)
    if not entry:
        return {"error": "Доступ запрещён или элемент не найден"}

    source_id = entry.get("source_profile")
    item_type = entry.get("type")
    item_id = entry.get("item_id")

    # Для опубликованных в общий доступ — читаем из shared-директории
    if item_type in PUBLISHABLE_TYPES:
        # Запись из share_with_profile (кросспрофильная ссылка) хранит
        # target_profile и НЕ копирует файл. Читаем у владельца, иначе
        # получатель видел «Файл не найден» (копии в shared нет).
        if entry.get("target_profile"):
            return _resolve_source_item(source_id, item_type, item_id)
        return _resolve_published_item(source_id, item_type, item_id)
    elif item_type in LINK_ONLY_TYPES:
        return _resolve_link_item(profile_id, entry)
    else:
        return {"error": f"Неизвестный тип: {item_type}"}


def _resolve_file_item(base_profile, item_type, item_id, shared=False):
    """Прочитать элемент из data-каталога профиля (опц. из подпапки shared)."""
    type_info = PUBLISHABLE_TYPES.get(item_type)
    if not type_info:
        return {"error": f"Неизвестный тип: {item_type}"}
    folder = os.path.join(_profile_data_dir(base_profile), type_info["dir"])
    if shared:
        folder = os.path.join(folder, "shared")
    if type_info["single_file"]:
        fname = item_id + (type_info["ext"] or "")
        fpath = os.path.join(folder, fname)
        if not os.path.exists(fpath):
            return {"error": "Файл не найден"}
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            return {"type": item_type, "item_id": item_id, "content": content, "path": fpath}
        except Exception as e:
            swallow.note('share_module._resolve_file_item:L419', e)
            return {"error": f"Ошибка чтения: {e}"}
    else:
        dir_path = os.path.join(folder, item_id)
        if not os.path.isdir(dir_path):
            return {"error": "Папка не найдена"}
        return {"type": item_type, "item_id": item_id, "path": dir_path, "is_dir": True}


def _resolve_source_item(source_id, item_type, item_id):
    """Прочитать элемент напрямую у профиля-владельца (кросспрофильная ссылка)."""
    if not source_id:
        return {"error": "Не указан профиль-владелец"}
    return _resolve_file_item(source_id, item_type, item_id, shared=False)


def _resolve_published_item(source_id, item_type, item_id):
    """Прочитать опубликованный файл из shared-директории."""
    default_id = _get_default_profile_id()
    if not default_id:
        return {"error": "Нет общедоступного профиля"}
    return _resolve_file_item(default_id, item_type, item_id, shared=True)


def _resolve_link_item(profile_id, entry):
    """Прочитать элемент по кросспрофильной ссылке."""
    source_id = entry.get("source_profile")
    item_type = entry.get("type")
    item_id = entry.get("item_id")

    if item_type in ("music", "video"):
        # Возвращаем информацию о треке/видео для media_module
        return {
            "type": item_type,
            "item_id": item_id,
            "source_profile": source_id,
            "note": "Используйте /api/media/* с source_profile параметром",
        }
    elif item_type == "photo":
        return {
            "type": "photo",
            "item_id": item_id,
            "source_profile": source_id,
            "note": "Используйте /api/photos/* с source_profile параметром",
        }
    elif item_type == "zim":
        return {
            "type": "zim",
            "item_id": item_id,
            "source_profile": source_id,
        }
    elif item_type == "book":
        return {
            "type": "book",
            "item_id": item_id,
            "source_profile": source_id,
        }
    elif item_type == "collection_book":
        return {
            "type": "collection_book",
            "book_id": item_id,
            "source_profile": source_id,
        }

    return {"error": "Не удалось определить тип контента"}
