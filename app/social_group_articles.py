"""Статьи группы или канала.

Отдельный модуль по той же причине, что и альбомы: social_module.py перевалил
за 6000 строк, а блок статей крупный.

Статья состоит из заголовка, обложки и произвольного числа секций. Формат
разметки внутри секции — Markdown: в плане на вопрос о формате выбрано
«и то и то», то есть для Markdown есть предпросмотр без редактора, а
WYSIWYG переводится в Markdown перед сохранением. В базе хранится один
формат — Markdown, иначе пришлось бы поддерживать две ветки рендеринга.

Хранение обычным текстом: сервер не строит HTML и не отвечает за санитизацию
разметки — её делает клиент перед вставкой через innerHTML.

Статья принадлежит либо группе, либо каналу сервера; область определяется
через social_scope.Scope, поэтому логика прав одна и та же.
"""
import os
import time
import uuid

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import social_module as sm
import social_scope as scope_mod

router = APIRouter(tags=["social-articles"])

# Публикация, черновик, на проверке, отклонён.
STATUSES = ("draft", "published", "review", "rejected")
_COVER_EXTS = (set(sm._ALLOWED_IMAGE_EXT) | {".mp4", ".webm", ".mov", ".avi"}
               | {".mp3", ".ogg", ".wav", ".m4a", ".flac"})
_MAX_SECTION_BYTES = 512 * 1024
_MAX_SECTIONS = 50


def _require_member(conn, group_id, profile_id):
    return scope_mod.group_scope(conn, group_id, profile_id)


def _is_manager(scope):
    return scope.is_manager


def _load_scoped_article(conn, article_id, scope):
    """Загрузить статью и убедиться, что она принадлежит этой области."""
    row = conn.execute(
        "SELECT * FROM social_articles WHERE id=? AND %s=?" % scope.owner_col,
        (article_id, scope.owner_id)).fetchone()
    if not row:
        raise HTTPException(404, "Статья не найдена")
    return row


def _clean(value, limit):
    return (value or "").strip()[:limit]


def _owner_value(value):
    """Пустой идентификатор области — это NULL, а не пустая строка.

    У social_articles внешний ключ на group_id, и подстановка '' нарушала
    его при создании статьи в канале сервера.
    """
    return value or None


def _load_sections(conn, article_id):
    rows = conn.execute(
        "SELECT * FROM social_article_sections WHERE article_id=? ORDER BY position ASC",
        (article_id,)).fetchall()
    out = []
    for r in rows:
        d = sm._row_to_dict(r)
        # position — служебное поле сортировки, клиенту оно не нужно.
        d.pop("position", None)
        out.append(d)
    return out


def _save_sections(conn, article_id, sections):
    """Заменить набор секций целиком.

    Частичное обновление массива потребовало бы сопоставления по id, а в
    редакторе секции всё равно переписывают целиком.
    """
    conn.execute("DELETE FROM social_article_sections WHERE article_id=?", (article_id,))
    for i, sec in enumerate(sections[:_MAX_SECTIONS]):
        if not isinstance(sec, dict):
            continue
        title = _clean(sec.get("title"), 200)
        content = (sec.get("content") or "")[:_MAX_SECTION_BYTES]
        if not title and not content.strip():
            continue
        conn.execute(
            "INSERT INTO social_article_sections (id, article_id, title, content, position) "
            "VALUES (?,?,?,?,?)", (str(uuid.uuid4()), article_id, title, content, i))


def _list_articles(conn, profile_id, scope, status, include_drafts):
    clauses = [scope.owner_col + "=?"]
    values = [scope.owner_id]
    if status:
        clauses.append("status=?")
        values.append(status)
    elif not include_drafts:
        # Без явного фильтра видны опубликованные плюс свои черновики.
        clauses.append("(status='published' OR author_id=?)")
        values.append(profile_id)
    sql = ("SELECT * FROM social_articles WHERE " + " AND ".join(clauses) +
           " ORDER BY updated_at DESC")
    rows = conn.execute(sql, values).fetchall()
    out = []
    for r in rows:
        can_edit = scope.is_manager or r["author_id"] == profile_id
        if r["status"] == "draft" and not can_edit:
            continue
        d = sm._row_to_dict(r)
        d["can_edit"] = can_edit
        out.append(d)
    return JSONResponse({"articles": out})


@router.get("/groups/{group_id}/articles")
def list_articles(group_id: str, request: Request, status: str = "",
                        include_drafts: bool = False):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    if status and status not in STATUSES:
        raise HTTPException(400, "Неизвестный статус статьи")
    return _list_articles(conn, profile_id, scope, status, include_drafts)


def _create_article(conn, profile_id, scope, data):
    title = _clean(data.get("title"), 200)
    if not title:
        raise HTTPException(400, "Введите заголовок статьи")
    status = (data.get("status") or "draft").strip()
    if status not in STATUSES:
        raise HTTPException(400, "Неверный статус статьи")
    now = time.time()
    article_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_articles (id, group_id, channel_id, author_id, title, "
        "excerpt, cover_image, status, views_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
        (article_id, _owner_value(scope.group_id), _owner_value(scope.channel_id),
         profile_id, title,
         _clean(data.get("excerpt"), 500), _clean(data.get("cover_image"), 500),
         status, now, now))
    if isinstance(data.get("sections"), list):
        _save_sections(conn, article_id, data["sections"])
    conn.commit()
    return JSONResponse({"ok": True, "article_id": article_id})


@router.post("/groups/{group_id}/articles")
def create_article(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return _create_article(conn, profile_id, scope, data)


def _read_article(conn, profile_id, scope, article_id):
    row = _load_scoped_article(conn, article_id, scope)
    can_edit = scope.is_manager or row["author_id"] == profile_id
    if row["status"] == "draft" and not can_edit:
        # Черновик не должен утекать к участникам по прямой ссылке.
        raise HTTPException(403, "Статья не опубликована")
    if row["status"] == "published":
        conn.execute(
            "UPDATE social_articles SET views_count=views_count+1 WHERE id=?", (article_id,))
        conn.commit()
    out = sm._row_to_dict(row)
    out["sections"] = _load_sections(conn, article_id)
    out["can_edit"] = can_edit
    return JSONResponse(out)


@router.get("/groups/{group_id}/articles/{article_id}")
def get_article(group_id: str, article_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return _read_article(conn, profile_id, scope, article_id)


def _update_article(conn, profile_id, scope, article_id, data):
    row = _load_scoped_article(conn, article_id, scope)
    if not (scope.is_manager or row["author_id"] == profile_id):
        raise HTTPException(403, "Нет прав на изменение статьи")

    fields, values = [], []
    if "title" in data:
        title = _clean(data["title"], 200)
        if not title:
            raise HTTPException(400, "Заголовок не может быть пустым")
        fields.append("title=?")
        values.append(title)
    for col, limit in (("excerpt", 500), ("cover_image", 500)):
        if col in data:
            fields.append(f"{col}=?")
            values.append(_clean(data[col], limit))
    if "status" in data:
        status = str(data["status"]).strip()
        if status not in STATUSES:
            raise HTTPException(400, "Неверный статус статьи")
        fields.append("status=?")
        values.append(status)
    if isinstance(data.get("sections"), list):
        _save_sections(conn, article_id, data["sections"])

    if fields:
        fields.append("updated_at=?")
        values.append(time.time())
        values.append(article_id)
        conn.execute(f"UPDATE social_articles SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
    updated = conn.execute("SELECT * FROM social_articles WHERE id=?", (article_id,)).fetchone()
    out = sm._row_to_dict(updated)
    out["sections"] = _load_sections(conn, article_id)
    out["can_edit"] = True
    return JSONResponse({"ok": True, "article": out})


@router.put("/groups/{group_id}/articles/{article_id}")
def update_article(group_id: str, article_id: str, request: Request,
                         data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return _update_article(conn, profile_id, scope, article_id, data)


def _delete_article(conn, profile_id, scope, article_id):
    row = _load_scoped_article(conn, article_id, scope)
    if not (scope.is_manager or row["author_id"] == profile_id):
        raise HTTPException(403, "Нет прав на удаление статьи")
    conn.execute("DELETE FROM social_article_sections WHERE article_id=?", (article_id,))
    conn.execute("DELETE FROM social_articles WHERE id=?", (article_id,))
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/groups/{group_id}/articles/{article_id}")
def delete_article(group_id: str, article_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return _delete_article(conn, profile_id, scope, article_id)


def _upload_cover(conn, profile_id, scope, file):
    """Загрузить обложку статьи. Файл хранится рядом с остальными медиа,
    проверка формата — по расширению."""
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _COVER_EXTS:
        raise HTTPException(400, "Недопустимый формат обложки")
    # Префикс несёт область — так файлы группы и канала различаются в каталоге.
    prefix = "acov" if scope.kind == "group" else "ccov"
    filename = f"{prefix}{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(sm.UPLOADS_DIR, filename)
    sm._save_upload(file, filepath, sm._MAX_POST_MEDIA_BYTES,
                    image_only=(ext in sm._ALLOWED_IMAGE_EXT))
    return JSONResponse({"ok": True, "path": f"/api/social/file/root/{filename}"})


@router.post("/groups/{group_id}/articles/upload")
def upload_article_cover(group_id: str, request: Request,
                               file: UploadFile = File(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.group_scope(conn, group_id, profile_id)
    return _upload_cover(conn, profile_id, scope, file)


# --- Статьи каналов сервера ---
#
# Те же операции, что и для группы, отличается только определение области.
# Дублирования логики нет: она живёт в функциях выше.


@router.get("/servers/{server_id}/channels/{channel_id}/articles")
def list_channel_articles(server_id: str, channel_id: str, request: Request,
                                status: str = "", include_drafts: bool = False):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.channel_scope(conn, server_id, channel_id, profile_id)
    if status and status not in STATUSES:
        raise HTTPException(400, "Неизвестный статус статьи")
    return _list_articles(conn, profile_id, scope, status, include_drafts)


@router.post("/servers/{server_id}/channels/{channel_id}/articles")
def create_channel_article(server_id: str, channel_id: str, request: Request,
                                 data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.channel_scope(conn, server_id, channel_id, profile_id)
    return _create_article(conn, profile_id, scope, data)


@router.get("/servers/{server_id}/channels/{channel_id}/articles/{article_id}")
def get_channel_article(server_id: str, channel_id: str, article_id: str,
                              request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.channel_scope(conn, server_id, channel_id, profile_id)
    return _read_article(conn, profile_id, scope, article_id)


@router.put("/servers/{server_id}/channels/{channel_id}/articles/{article_id}")
def update_channel_article(server_id: str, channel_id: str, article_id: str,
                                 request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.channel_scope(conn, server_id, channel_id, profile_id)
    return _update_article(conn, profile_id, scope, article_id, data)


@router.delete("/servers/{server_id}/channels/{channel_id}/articles/{article_id}")
def delete_channel_article(server_id: str, channel_id: str, article_id: str,
                                 request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.channel_scope(conn, server_id, channel_id, profile_id)
    return _delete_article(conn, profile_id, scope, article_id)


@router.post("/servers/{server_id}/channels/{channel_id}/articles/upload")
def upload_channel_article_cover(server_id: str, channel_id: str, request: Request,
                                      file: UploadFile = File(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    scope = scope_mod.channel_scope(conn, server_id, channel_id, profile_id)
    return _upload_cover(conn, profile_id, scope, file)