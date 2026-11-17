"""Альбомы групп и каналов.

Отдельный модуль, потому что social_module.py вырос за 6000 строк, а блок
альбомов получается крупным.

Схема одна для профиля, для групп и для каналов сервера — в отличие от
первоначального плана, заводить отдельные таблицы для видео и аудио не нужно:
альбомы отличаются только типом содержимого. В social_albums добавлены
колонки:
  * group_id   — пусто для альбома профиля, идентификатор группы для её альбома
  * channel_id — идентификатор канала сервера (пусто для профиля и группы)
  * kind       — photo | video | audio

Владелец альбома определяется через social_scope.Scope: одна и та же логика
прав работает и для группы, и для канала сервера.

Приватность: 0 публичный, 1 только участники, 2 приватный (админы),
3 по ссылке.
"""
import os
import time
import uuid

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import social_module as sm
import social_scope as scope_mod

# Префикс "/api/social" НЕ указываем: он уже задан у social_module.router,
# и include_router добавил бы его второй раз, давая путь вида
# /api/social/api/social/... — такой маршрут просто не находился бы.
router = APIRouter(tags=["social-albums"])

KINDS = ("photo", "video", "audio")
# Что пропускает загрузчик для каждого типа альбома.
_KIND_EXTS = {
    "photo": sm._ALLOWED_IMAGE_EXT,
    "video": {".mp4", ".webm", ".mov", ".avi"},
    "audio": {".mp3", ".m4a", ".ogg", ".wav", ".flac"},
}
_KIND_MAX_BYTES = {
    "photo": sm._MAX_IMAGE_BYTES,
    "video": sm._MAX_POST_MEDIA_BYTES,
    "audio": sm._MAX_POST_MEDIA_BYTES,
}
PRIVACY_ALL = (0, 1, 2, 3)


def _is_manager(role):
    return role in ("owner", "admin", "moderator")


def _album_row(row):
    d = sm._row_to_dict(row)
    d["is_group"] = bool(d.get("group_id"))
    d["is_channel"] = bool(d.get("channel_id"))
    return d


def _load_scoped_album(conn, album_id, scope):
    """Загрузить альбом и убедиться, что он принадлежит именно этой области.

    Проверка по group_id/channel_id обязательна: по одному album_id альбом
    чужой группы или канала иначе был бы виден.
    """
    row = conn.execute("SELECT * FROM social_albums WHERE id=? AND %s=?" % scope.owner_col,
                       (album_id, scope.owner_id)).fetchone()
    if not row:
        raise HTTPException(404, "Альбом не найден")
    return row


@router.get("/groups/{group_id}/albums")
def list_group_albums(group_id: str, request: Request, kind: str = ""):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    if kind and kind not in KINDS:
        raise HTTPException(400, "Неизвестный тип альбома")
    frag, params = scope_mod.album_filter(scope, "kind=?" if kind else "")
    if kind:
        params.append(kind)
    rows = conn.execute(
        "SELECT * FROM social_albums WHERE %s ORDER BY created_at DESC" % frag,
        params).fetchall()
    # Приватные альбомы видны только администраторам: иначе участник группы
    # получил бы доступ к содержимому, которое ему не показывают.
    out = []
    for r in rows:
        if r["privacy"] in (2, 3) and not scope.is_manager:
            continue
        d = _album_row(r)
        d["can_edit"] = scope.is_manager or r["profile_id"] == profile_id
        out.append(d)
    return JSONResponse({"albums": out})


@router.post("/groups/{group_id}/albums")
async def create_group_album(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return await _create_album(conn, profile_id, scope, data)


async def _create_album(conn, profile_id, scope, data):
    """Общая часть создания альбома для группы и для канала.

    Колонка-владелец подставляется по scope, поэтому колонки group_id и
    channel_id заполняются по очереди, а не одновременно.
    """
    # Создавать альбомы может любой участник: заполнить его сможет автор,
    # удалить — только он или администратор.
    title = (data.get("title") or "").strip()[:100]
    if not title:
        raise HTTPException(400, "Введите название альбома")
    kind = (data.get("kind") or "photo").strip()
    if kind not in KINDS:
        raise HTTPException(400, "Тип альбома: photo, video или audio")
    privacy = data.get("privacy")
    try:
        privacy = int(privacy) if privacy is not None else 0
    except (TypeError, ValueError):
        raise HTTPException(400, "Неверный параметр приватности")
    if privacy not in PRIVACY_ALL:
        raise HTTPException(400, "Неверный параметр приватности")
    now = time.time()
    album_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_albums (id, profile_id, group_id, channel_id, title, "
        "description, created_at, updated_at, photo_count, privacy, kind) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
        (album_id, profile_id, scope.group_id, scope.channel_id, title,
         (data.get("description") or "")[:500], now, now, privacy, kind))
    conn.commit()
    row = conn.execute("SELECT * FROM social_albums WHERE id=?", (album_id,)).fetchone()
    return JSONResponse({"ok": True, "album": _album_row(row)})


async def _get_album(conn, profile_id, scope, album_id):
    album = _load_scoped_album(conn, album_id, scope)
    if album["privacy"] in (2, 3) and not scope.is_manager:
        raise HTTPException(403, "Нет доступа к альбому")
    # Автор, аватар, счётчик лайков и «понравилось ли мне» тянутся одним
    # JOIN: панель просмотрщика показывает их сразу для всех фото альбома,
    # и по отдельному запросу на каждое фото получилось бы N+1 запросов
    # при каждом открытии альбома.
    photos = conn.execute(
        "SELECT p.id AS photo_id, p.url, p.caption, ap.media_type, "
        "       p.profile_id AS author_id, p.created_at, "
        "       COALESCE(u.display_name, p.profile_id) AS author_name, "
        "       u.avatar_path AS author_avatar, u.avatar_crop AS author_crop, "
        "       (SELECT COUNT(*) FROM social_photo_likes l WHERE l.photo_id = p.id) AS likes_count, "
        "       EXISTS(SELECT 1 FROM social_photo_likes l2 "
        "              WHERE l2.photo_id = p.id AND l2.profile_id = ?) AS liked_by_me "
        "FROM social_album_photos ap "
        "JOIN social_profile_photos p ON p.id = ap.photo_id "
        "LEFT JOIN social_users u ON u.profile_id = p.profile_id "
        "WHERE ap.album_id=? ORDER BY ap.sort_order ASC", (profile_id, album_id)).fetchall()
    out = _album_row(album)
    can_edit = scope.is_manager or album["profile_id"] == profile_id
    out["photos"] = [_photo_dict(p, can_edit) for p in photos]
    out["can_edit"] = can_edit
    return JSONResponse(out)


def _photo_dict(p, album_can_edit):
    """Строка фото для панели просмотрщика."""
    d = sm._row_to_dict(p)
    d["likes"] = d.get("likes_count") or 0
    d["liked"] = bool(d.get("liked_by_me"))
    d["can_edit"] = bool(album_can_edit)
    d["can_delete"] = bool(album_can_edit)
    return d


@router.get("/groups/{group_id}/albums/{album_id}")
async def get_group_album(group_id: str, album_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return await _get_album(conn, profile_id, scope, album_id)


async def _upload_photo(conn, profile_id, scope, album_id, file):
    album = _load_scoped_album(conn, album_id, scope)
    if not (scope.is_manager or album["profile_id"] == profile_id):
        raise HTTPException(403, "Нет прав на изменение альбома")
    kind = album["kind"] or "photo"
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _KIND_EXTS[kind]:
        allowed = ", ".join(sorted(_KIND_EXTS[kind]))
        raise HTTPException(400, "Недопустимый формат для этого типа альбома (%s)" % allowed)
    # Префикс файла несёт область: так файлы группы отличаются от файлов
    # канала при беглом просмотре каталога загрузок.
    prefix = "grp" if scope.kind == "group" else "chn"
    filename = f"{prefix}{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(sm.UPLOADS_DIR, filename)
    sm._save_upload(file, filepath, _KIND_MAX_BYTES[kind], image_only=(kind == "photo"))
    url = f"/api/social/file/root/{filename}"
    now = time.time()
    # Подпись = исходное имя файла без расширения: в аудио- и видео-альбомах
    # по ней показывается название дорожки/ролика. Для фото не отображается,
    # но это полезная метадана.
    caption = os.path.splitext(os.path.basename(file.filename or ""))[0].strip()[:120]
    photo_id = str(uuid.uuid4())
    conn.execute(
        # origin='album' помечает фото как принадлежащее альбому, а не
        # профилю: иначе оно всплыло бы в профильной галерее автора.
        "INSERT INTO social_profile_photos (id, profile_id, url, caption, created_at, origin) "
        "VALUES (?,?,?,?,?,'album')", (photo_id, profile_id, url, caption, now))
    conn.execute(
        "INSERT OR IGNORE INTO social_album_photos (album_id, photo_id, sort_order, "
        "added_at, media_type) VALUES (?,?,?,?,?)",
        (album_id, photo_id, int(time.time()), now, kind))
    _refresh_album_stats(conn, album_id)
    conn.commit()
    return JSONResponse({"ok": True, "photo_id": photo_id, "url": url, "type": kind})


@router.post("/groups/{group_id}/albums/{album_id}/photos/upload")
async def upload_group_album_photo(group_id: str, album_id: str, request: Request,
                                   file: UploadFile = File(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return await _upload_photo(conn, profile_id, scope, album_id, file)


async def _add_existing_photo(conn, profile_id, scope, album_id, data):
    """Добавить в альбом уже загруженное фото пользователя."""
    album = _load_scoped_album(conn, album_id, scope)
    if not (scope.is_manager or album["profile_id"] == profile_id):
        raise HTTPException(403, "Нет прав на изменение альбома")
    photo_id = (data.get("photo_id") or "").strip()
    photo = conn.execute(
        "SELECT id FROM social_profile_photos WHERE id=? AND profile_id=?",
        (photo_id, profile_id)).fetchone()
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    conn.execute(
        "INSERT OR IGNORE INTO social_album_photos (album_id, photo_id, sort_order, "
        "added_at, media_type) VALUES (?,?,?,?,?)",
        (album_id, photo_id, int(time.time()), time.time(), album["kind"] or "photo"))
    _refresh_album_stats(conn, album_id)
    conn.commit()
    return JSONResponse({"ok": True, "photo_id": photo_id})


@router.post("/groups/{group_id}/albums/{album_id}/photos")
async def add_existing_photo(group_id: str, album_id: str, request: Request,
                             data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return await _add_existing_photo(conn, profile_id, scope, album_id, data)


async def _remove_photo(conn, profile_id, scope, album_id, photo_id):
    album = _load_scoped_album(conn, album_id, scope)
    if not (scope.is_manager or album["profile_id"] == profile_id):
        raise HTTPException(403, "Нет прав на изменение альбома")
    conn.execute("DELETE FROM social_album_photos WHERE album_id=? AND photo_id=?",
                 (album_id, photo_id))
    _refresh_album_stats(conn, album_id)
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/groups/{group_id}/albums/{album_id}/photos/{photo_id}")
async def remove_group_album_photo(group_id: str, album_id: str, photo_id: str,
                                   request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return await _remove_photo(conn, profile_id, scope, album_id, photo_id)


async def _delete_album(conn, profile_id, scope, album_id):
    album = _load_scoped_album(conn, album_id, scope)
    if not (scope.is_manager or album["profile_id"] == profile_id):
        raise HTTPException(403, "Нет прав на удаление альбома")
    # Файлы физически не удаляем: на них могут ссылаться посты и сообщения,
    # и удаление сломало бы их отображение.
    conn.execute("DELETE FROM social_album_photos WHERE album_id=?", (album_id,))
    conn.execute("DELETE FROM social_albums WHERE id=?", (album_id,))
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/groups/{group_id}/albums/{album_id}")
async def delete_group_album(group_id: str, album_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return await _delete_album(conn, profile_id, scope, album_id)


@router.put("/groups/{group_id}/albums/{album_id}")
def update_group_album(group_id: str, album_id: str, request: Request,
                             data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return _update_album(conn, profile_id, scope, album_id, data)


def _update_album(conn, profile_id, scope, album_id, data):
    album = _load_scoped_album(conn, album_id, scope)
    if not (scope.is_manager or album["profile_id"] == profile_id):
        raise HTTPException(403, "Нет прав на изменение альбома")
    fields, values = [], []
    for col, limit in (("title", 100), ("description", 500), ("cover_path", 500)):
        if col in data:
            val = (data.get(col) or "").strip()[:limit]
            if col == "title" and not val:
                raise HTTPException(400, "Название не может быть пустым")
            fields.append(f"{col}=?")
            values.append(val)
    if "privacy" in data:
        try:
            privacy = int(data["privacy"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Неверный параметр приватности")
        if privacy not in PRIVACY_ALL:
            raise HTTPException(400, "Неверный параметр приватности")
        fields.append("privacy=?")
        values.append(privacy)
    if not fields:
        return JSONResponse({"ok": True})
    fields.append("updated_at=?")
    values.append(time.time())
    values.append(album_id)
    conn.execute(f"UPDATE social_albums SET {','.join(fields)} WHERE id=?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM social_albums WHERE id=?", (album_id,)).fetchone()
    return JSONResponse({"ok": True, "album": _album_row(row)})


def _refresh_album_stats(conn, album_id):
    """Пересчитать счётчик и обложку после изменения состава."""
    count = conn.execute(
        "SELECT COUNT(*) FROM social_album_photos WHERE album_id=?",
        (album_id,)).fetchone()[0]
    conn.execute("UPDATE social_albums SET photo_count=? WHERE id=?", (count, album_id))
    if count == 0:
        conn.execute("UPDATE social_albums SET cover_path='' WHERE id=?", (album_id,))
        return
    row = conn.execute(
        "SELECT cover_path FROM social_albums WHERE id=?", (album_id,)).fetchone()
    if row["cover_path"]:
        return
    first = conn.execute(
        "SELECT p.url FROM social_album_photos ap "
        "JOIN social_profile_photos p ON p.id=ap.photo_id "
        "WHERE ap.album_id=? ORDER BY ap.sort_order ASC LIMIT 1",
        (album_id,)).fetchone()
    if first:
        conn.execute("UPDATE social_albums SET cover_path=? WHERE id=?",
                     (first["url"], album_id))


# --- Альбомы каналов сервера ---
#
# Маршруты повторяют групповые один в один и отличаются только тем, как
# определяется область. Сами операции не дублируются: все они живут в
# функциях _list/_get/_create/... и вызываются отсюда через scope.


def _channel_scope(conn, server_id, channel_id, profile_id):
    return scope_mod.channel_scope(conn, server_id, channel_id, profile_id)


@router.get("/servers/{server_id}/channels/{channel_id}/albums")
def list_channel_albums(server_id: str, channel_id: str, request: Request,
                              kind: str = ""):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    if kind and kind not in KINDS:
        raise HTTPException(400, "Неизвестный тип альбома")
    frag, params = scope_mod.album_filter(scope, "kind=?" if kind else "")
    if kind:
        params.append(kind)
    rows = conn.execute(
        "SELECT * FROM social_albums WHERE %s ORDER BY created_at DESC" % frag,
        params).fetchall()
    out = []
    for r in rows:
        if r["privacy"] in (2, 3) and not scope.is_manager:
            continue
        d = _album_row(r)
        d["can_edit"] = scope.is_manager or r["profile_id"] == profile_id
        out.append(d)
    return JSONResponse({"albums": out})


@router.post("/servers/{server_id}/channels/{channel_id}/albums")
async def create_channel_album(server_id: str, channel_id: str, request: Request,
                               data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return await _create_album(conn, profile_id, scope, data)


@router.get("/servers/{server_id}/channels/{channel_id}/albums/{album_id}")
async def get_channel_album(server_id: str, channel_id: str, album_id: str,
                            request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return await _get_album(conn, profile_id, scope, album_id)


@router.post("/servers/{server_id}/channels/{channel_id}/albums/{album_id}/photos/upload")
async def upload_channel_album_photo(server_id: str, channel_id: str, album_id: str,
                                     request: Request, file: UploadFile = File(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return await _upload_photo(conn, profile_id, scope, album_id, file)


@router.post("/servers/{server_id}/channels/{channel_id}/albums/{album_id}/photos")
async def add_channel_existing_photo(server_id: str, channel_id: str, album_id: str,
                                     request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return await _add_existing_photo(conn, profile_id, scope, album_id, data)


@router.delete(
    "/servers/{server_id}/channels/{channel_id}/albums/{album_id}/photos/{photo_id}")
async def remove_channel_album_photo(server_id: str, channel_id: str, album_id: str,
                                     photo_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return await _remove_photo(conn, profile_id, scope, album_id, photo_id)


@router.put("/servers/{server_id}/channels/{channel_id}/albums/{album_id}")
def update_channel_album(server_id: str, channel_id: str, album_id: str,
                               request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return _update_album(conn, profile_id, scope, album_id, data)


@router.delete("/servers/{server_id}/channels/{channel_id}/albums/{album_id}")
async def delete_channel_album(server_id: str, channel_id: str, album_id: str,
                               request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = _channel_scope(conn, server_id, channel_id, profile_id)
    return await _delete_album(conn, profile_id, scope, album_id)