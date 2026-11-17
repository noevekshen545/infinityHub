"""Форум групп: разделы, темы и ответы.

Отдельный модуль по той же причине, что social_group_albums: social_module.py
уже больше 6400 строк.

Структура хранения:

    social_forum_sections   раздел внутри группы («Общение», «Помощь»)
    social_forum_threads    тема, принадлежит разделу (раздел может быть пустым)
    social_forum_posts      сообщения темы; первый (is_first=1) — тело темы
    social_forum_post_likes лайки сообщений

Первый пост темы хранится в той же таблице, что и ответы. Иначе для тела
пришлось бы писать вторую ветку прав, вторую ветку лайков и второй формат
вывода. Так тело и ответы обрабатываются одинаково.

Отличие от альбомов и статей: форум живёт только в группах. Каналы сервера
транслируют, им форум не нужен, поэтому social_scope здесь не используется —
вместо него прямая проверка членства через social_group_members.

Права:
    * читать форум, создавать темы и отвечать — любой участник группы;
    * править своё, удалять своё — автор;
    * удалять чужое, закреплять, закрывать — админ/модератор;
    * разделы создают, переименовывают и удаляют — только админ.
"""
import json
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

import social_module as sm
import swallow

# Префикс "/api/social" уже задан у social_module.router — второй раз его
# задавать нельзя, иначе путь станет /api/social/api/social/...
router = APIRouter(tags=["social-forum"])

MAX_TITLE = 160
MAX_SECTION_TITLE = 80
MAX_POST_CHARS = 20000
THREADS_PAGE = 50
POSTS_PAGE = 20
SORT_SQL = {
    "new": "t.created_at DESC",
    "active": "t.updated_at DESC",
    "views": "t.views_count DESC",
    "posts": "t.posts_count DESC",
}


def _now():
    return sm._now()


def _require_member(conn, group_id, profile_id):
    """Проверить членство и вернуть роль.

    Возвращаем именно роль, а не Scope: форум не поддерживает каналы
    сервера, и вся абстракция social_scope тут была бы лишним слоем.
    """
    row = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
        (group_id, profile_id)).fetchone()
    if not row:
        raise HTTPException(403, "Вы не участник группы")
    return row["role"]


def _require_manager(conn, group_id, profile_id):
    role = _require_member(conn, group_id, profile_id)
    if role not in ("owner", "admin", "moderator"):
        raise HTTPException(403, "Нужны права администратора")
    return role


def _require_group(conn, group_id):
    row = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Группа не найдена")
    return row


def _author_cols(prefix=""):
    """Фрагмент SELECT с данными автора через JOIN на social_users."""
    p = prefix
    return (
        f"{p}author_id, "
        f"COALESCE(u.display_name, {p}author_id) AS display_name, "
        f"u.avatar_path AS author_avatar, u.avatar_crop AS author_crop"
    )


def _thread_row(row):
    d = sm._row_to_dict(row)
    d["is_pinned"] = bool(d.get("is_pinned"))
    d["is_closed"] = bool(d.get("is_closed"))
    return d


def _load_thread(conn, thread_id):
    row = conn.execute(
        "SELECT * FROM social_forum_threads WHERE id=?", (thread_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Тема не найдена")
    return row


def _can_moderate(role):
    return role in ("owner", "admin", "moderator")


def _can_delete(row, profile_id, role):
    return row["author_id"] == profile_id or _can_moderate(role)


def _attachments(raw):
    """Разобрать JSON вложений, не роняя ответ при битой строке."""
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        swallow.note('social_group_forum._attachments:L124')
        return []
    return data if isinstance(data, list) else []


def _clean(text, limit, field="Текст"):
    """Обрезать и проверить. Возвращает обрезанное значение."""
    value = (text or "").strip()
    if not value:
        raise HTTPException(400, f"{field} не может быть пустым")
    if len(value) > limit:
        raise HTTPException(400, f"{field} длиннее {limit} символов")
    return value


# ---------------------------------------------------------------- разделы


@router.get("/groups/{group_id}/forum")
def get_forum(group_id: str, request: Request):
    """Разделы форума вместе со счётчиками тем."""
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    role = _require_member(conn, group_id, profile_id)
    _require_group(conn, group_id)

    sections = conn.execute(
        "SELECT s.*, "
        "  (SELECT COUNT(*) FROM social_forum_threads t "
        "     WHERE t.section_id = s.id) AS threads_count, "
        "  (SELECT COUNT(*) FROM social_forum_posts p "
        "     JOIN social_forum_threads t2 ON t2.id = p.thread_id "
        "     WHERE t2.section_id = s.id) AS posts_count "
        "FROM social_forum_sections s "
        "WHERE s.group_id=? ORDER BY s.position, s.created_at",
        (group_id,)).fetchall()
    rows = [sm._row_to_dict(r) for r in sections]
    for d in rows:
        d["can_edit"] = role in ("owner", "admin")

    totals = conn.execute(
        "SELECT COUNT(*) AS threads, COALESCE(SUM(posts_count),0) AS posts "
        "FROM social_forum_threads WHERE group_id=?", (group_id,)).fetchone()

    return JSONResponse({
        "sections": rows,
        "threads_total": totals["threads"] or 0,
        "posts_total": totals["posts"] or 0,
        "is_manager": _can_moderate(role),
        "role": role,
    })


@router.post("/groups/{group_id}/forum/sections")
def create_section(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    _require_manager(conn, group_id, profile_id)
    title = _clean(data.get("title"), MAX_SECTION_TITLE, "Название раздела")
    description = (data.get("description") or "").strip()[:500]

    section_id = uuid.uuid4().hex[:12]
    # position в конец: порядок задаётся админом перетаскиванием.
    pos = conn.execute(
        "SELECT COALESCE(MAX(position),0)+1 FROM social_forum_sections WHERE group_id=?",
        (group_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO social_forum_sections (id, group_id, title, description, position, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (section_id, group_id, title, description, pos, _now()))
    conn.commit()
    row = conn.execute("SELECT * FROM social_forum_sections WHERE id=?", (section_id,)).fetchone()
    return JSONResponse(sm._row_to_dict(row))


@router.put("/groups/{group_id}/forum/sections/{section_id}")
def update_section(group_id: str, section_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    _require_manager(conn, group_id, profile_id)
    row = conn.execute(
        "SELECT * FROM social_forum_sections WHERE id=? AND group_id=?",
        (section_id, group_id)).fetchone()
    if not row:
        raise HTTPException(404, "Раздел не найден")

    title = row["title"]
    if "title" in data:
        title = _clean(data.get("title"), MAX_SECTION_TITLE, "Название раздела")
    description = row["description"] if "description" not in data \
        else (data.get("description") or "").strip()[:500]
    position = row["position"]
    if "position" in data:
        try:
            position = max(0, int(data.get("position") or 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "Некорректная позиция")

    conn.execute(
        "UPDATE social_forum_sections SET title=?, description=?, position=? WHERE id=?",
        (title, description, position, section_id))
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/groups/{group_id}/forum/sections/{section_id}")
def delete_section(group_id: str, section_id: str, request: Request):
    """Удалить раздел. Темы не удаляются, а остаются без раздела."""
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    _require_manager(conn, group_id, profile_id)
    row = conn.execute(
        "SELECT id FROM social_forum_sections WHERE id=? AND group_id=?",
        (section_id, group_id)).fetchone()
    if not row:
        raise HTTPException(404, "Раздел не найден")
    conn.execute("UPDATE social_forum_threads SET section_id='' WHERE section_id=?", (section_id,))
    conn.execute("DELETE FROM social_forum_sections WHERE id=?", (section_id,))
    conn.commit()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------- темы


@router.get("/groups/{group_id}/forum/threads")
def list_threads(group_id: str, request: Request,
                       section_id: str = "", q: str = "", sort: str = "active",
                       offset: int = 0, limit: int = THREADS_PAGE):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    role = _require_member(conn, group_id, profile_id)

    order = SORT_SQL.get(sort, SORT_SQL["active"])
    where = ["t.group_id=?"]
    params = [group_id]
    if section_id:
        where.append("t.section_id=?")
        params.append(section_id)
    if q:
        where.append("t.title LIKE ?")
        params.append("%" + q.strip() + "%")

    clause = " AND ".join(where)
    rows = conn.execute(
        f"SELECT t.*, s.title AS section_title, {_author_cols('t.')}, "
        f"  (SELECT display_name FROM social_users WHERE profile_id = "
        f"     (SELECT author_id FROM social_forum_posts WHERE thread_id = t.id "
        f"      ORDER BY created_at DESC LIMIT 1)) AS last_author, "
        f"  (SELECT MAX(created_at) FROM social_forum_posts WHERE thread_id = t.id) AS last_post_at "
        f"FROM social_forum_threads t "
        f"LEFT JOIN social_forum_sections s ON s.id = t.section_id "
        f"LEFT JOIN social_users u ON u.profile_id = t.author_id "
        f"WHERE {clause} "
        f"ORDER BY t.is_pinned DESC, {order} LIMIT ? OFFSET ?",
        params + [max(1, min(int(limit or THREADS_PAGE), 100)), max(0, int(offset or 0))]
    ).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) FROM social_forum_threads t WHERE {clause}", params).fetchone()[0]

    threads = [_thread_row(r) for r in rows]
    for t in threads:
        t["can_moderate"] = _can_moderate(role)

    return JSONResponse({
        "threads": threads,
        "total": total,
        "offset": max(0, int(offset or 0)),
        "limit": max(1, min(int(limit or THREADS_PAGE), 100)),
    })


@router.post("/groups/{group_id}/forum/threads")
def create_thread(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    _require_member(conn, group_id, profile_id)
    group = _require_group(conn, group_id)

    # post_permission: 0 — все участники, 1 — модераторы, 2 — админы.
    # Форум наследует то же правило, что и стена группы.
    permission = group["post_permission"] or 0
    if permission == 2 and profile_id != group["owner_id"]:
        member = conn.execute(
            "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
            (group_id, profile_id)).fetchone()
        if not member or member["role"] != "admin":
            raise HTTPException(403, "Публиковать могут только администраторы")
    elif permission == 1:
        member = conn.execute(
            "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
            (group_id, profile_id)).fetchone()
        if not member or not _can_moderate(member["role"]):
            raise HTTPException(403, "Публиковать могут только модераторы")

    title = _clean(data.get("title"), MAX_TITLE, "Заголовок")
    body = _clean(data.get("content"), MAX_POST_CHARS)
    attachments_raw = data.get("attachments") or "[]"
    if not isinstance(attachments_raw, str):
        attachments_raw = json.dumps(attachments_raw, ensure_ascii=False)
    attachments = sm._safe_attachments(attachments_raw)

    section_id = (data.get("section_id") or "").strip()
    if section_id:
        ok = conn.execute(
            "SELECT 1 FROM social_forum_sections WHERE id=? AND group_id=?",
            (section_id, group_id)).fetchone()
        if not ok:
            raise HTTPException(400, "Раздел не найден")

    thread_id = uuid.uuid4().hex[:12]
    post_id = uuid.uuid4().hex[:12]
    now = _now()
    conn.execute(
        "INSERT INTO social_forum_threads "
        "(id, group_id, section_id, author_id, title, created_at, updated_at, posts_count) "
        "VALUES (?,?,?,?,?,?,?,1)",
        (thread_id, group_id, section_id, profile_id, title, now, now))
    conn.execute(
        "INSERT INTO social_forum_posts "
        "(id, thread_id, group_id, author_id, content, created_at, is_first, attachments) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (post_id, thread_id, group_id, profile_id, body, now, attachments))
    # Упоминания в теле темы: уведомление ведёт в тему форума.
    sm._parse_mentions(body, post_id, profile_id, conn, "forum_thread", thread_id)
    conn.execute("UPDATE social_groups SET last_activity=? WHERE id=?", (now, group_id))
    conn.commit()
    return JSONResponse({"ok": True, "thread_id": thread_id, "post_id": post_id})


@router.get("/forum/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request, offset: int = 0, limit: int = POSTS_PAGE):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    thread = _load_thread(conn, thread_id)
    role = _require_member(conn, thread["group_id"], profile_id)

    conn.execute(
        "UPDATE social_forum_threads SET views_count=views_count+1 WHERE id=?", (thread_id,))
    conn.commit()
    # Счётчик берём прибавлением к уже загруженному значению: строка
    # thread прочитана ДО инкремента, и отдавать устаревшее значение
    # означало бы, что первый просмотр не учитывается.
    views = (thread["views_count"] or 0) + 1

    limit = max(1, min(int(limit or POSTS_PAGE), 100))
    offset = max(0, int(offset or 0))
    rows = conn.execute(
        f"SELECT p.*, {_author_cols('p.')}, "
        f"  EXISTS(SELECT 1 FROM social_forum_post_likes l "
        f"         WHERE l.post_id=p.id AND l.profile_id=?) AS liked_by_me "
        f"FROM social_forum_posts p "
        f"LEFT JOIN social_users u ON u.profile_id = p.author_id "
        f"WHERE p.thread_id=? ORDER BY p.created_at LIMIT ? OFFSET ?",
        (profile_id, thread_id, limit, offset)).fetchall()

    total = conn.execute(
        "SELECT COUNT(*) FROM social_forum_posts WHERE thread_id=?", (thread_id,)).fetchone()[0]

    posts = [sm._row_to_dict(r) for r in rows]
    for p in posts:
        p["attachments"] = _attachments(p.get("attachments"))
        p["can_delete"] = _can_delete(
            {"author_id": p["author_id"]}, profile_id, role)
        p["can_edit"] = p["author_id"] == profile_id

    t = _thread_row(thread)
    t["views_count"] = views
    t["section_title"] = ""
    if t.get("section_id"):
        s = conn.execute("SELECT title FROM social_forum_sections WHERE id=?",
                         (t["section_id"],)).fetchone()
        t["section_title"] = s["title"] if s else ""
    t["can_moderate"] = _can_moderate(role)
    t["can_delete"] = _can_delete({"author_id": t["author_id"]}, profile_id, role)
    t["can_edit"] = t["author_id"] == profile_id

    return JSONResponse({
        "thread": t,
        "posts": posts,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(posts) < total,
    })


@router.put("/forum/threads/{thread_id}")
def update_thread(thread_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    thread = _load_thread(conn, thread_id)
    role = _require_member(conn, thread["group_id"], profile_id)
    if thread["author_id"] != profile_id and not _can_moderate(role):
        raise HTTPException(403, "Можно править только свою тему")

    title = _clean(data.get("title"), MAX_TITLE, "Заголовок")
    section_id = thread["section_id"]
    if "section_id" in data:
        section_id = (data.get("section_id") or "").strip()
        if section_id:
            ok = conn.execute(
                "SELECT 1 FROM social_forum_sections WHERE id=? AND group_id=?",
                (section_id, thread["group_id"])).fetchone()
            if not ok:
                raise HTTPException(400, "Раздел не найден")
    conn.execute(
        "UPDATE social_forum_threads SET title=?, section_id=?, updated_at=? WHERE id=?",
        (title, section_id, _now(), thread_id))
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/forum/threads/{thread_id}")
def delete_thread(thread_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    thread = _load_thread(conn, thread_id)
    role = _require_member(conn, thread["group_id"], profile_id)
    if not _can_delete({"author_id": thread["author_id"]}, profile_id, role):
        raise HTTPException(403, "Недостаточно прав")

    # Посты и лайки уходят каскадом (FK ... ON DELETE CASCADE).
    conn.execute("DELETE FROM social_forum_threads WHERE id=?", (thread_id,))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/forum/threads/{thread_id}/pin")
def toggle_pin(thread_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    thread = _load_thread(conn, thread_id)
    _require_manager(conn, thread["group_id"], profile_id)
    conn.execute(
        "UPDATE social_forum_threads SET is_pinned=1-is_pinned WHERE id=?", (thread_id,))
    conn.commit()
    row = conn.execute("SELECT is_pinned FROM social_forum_threads WHERE id=?", (thread_id,)).fetchone()
    return JSONResponse({"is_pinned": bool(row["is_pinned"])})


@router.post("/forum/threads/{thread_id}/close")
def toggle_close(thread_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    thread = _load_thread(conn, thread_id)
    _require_manager(conn, thread["group_id"], profile_id)
    conn.execute(
        "UPDATE social_forum_threads SET is_closed=1-is_closed WHERE id=?", (thread_id,))
    conn.commit()
    row = conn.execute("SELECT is_closed FROM social_forum_threads WHERE id=?", (thread_id,)).fetchone()
    return JSONResponse({"is_closed": bool(row["is_closed"])})


# ------------------------------------------------------------------ ответы


@router.get("/forum/threads/{thread_id}/posts")
async def list_posts(thread_id: str, request: Request,
                     offset: int = 0, limit: int = POSTS_PAGE):
    return await get_thread(thread_id, request, offset, limit)


@router.post("/forum/threads/{thread_id}/posts")
async def create_post(thread_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    thread = _load_thread(conn, thread_id)
    _require_member(conn, thread["group_id"], profile_id)
    if thread["is_closed"]:
        raise HTTPException(403, "Тема закрыта")

    # Текст может быть пустым, если есть вложение, поэтому _clean здесь не
    # подходит: он требует непустое значение.
    body = (data.get("content") or "").strip()
    if len(body) > MAX_POST_CHARS:
        raise HTTPException(400, "Текст длиннее %d символов" % MAX_POST_CHARS)
    # Вложения необязательны: ответ может быть только текстом.
    attachments_raw = data.get("attachments") or "[]"
    if not isinstance(attachments_raw, str):
        attachments_raw = json.dumps(attachments_raw, ensure_ascii=False)
    attachments = sm._safe_attachments(attachments_raw)
    if not body and attachments == "[]":
        raise HTTPException(400, "Текст или вложение обязательны")
    post_id = uuid.uuid4().hex[:12]
    now = _now()
    conn.execute(
        "INSERT INTO social_forum_posts "
        "(id, thread_id, group_id, author_id, content, created_at, attachments) VALUES (?,?,?,?,?,?,?)",
        (post_id, thread_id, thread["group_id"], profile_id, body, now, attachments))
    conn.execute(
        "UPDATE social_forum_threads SET updated_at=?, posts_count=posts_count+1 WHERE id=?",
        (now, thread_id))
    sm._parse_mentions(body, post_id, profile_id, conn, "forum_post", thread_id)
    conn.commit()
    row = conn.execute(
        f"SELECT p.*, {_author_cols('p.')}, 0 AS liked_by_me "
        f"FROM social_forum_posts p LEFT JOIN social_users u ON u.profile_id = p.author_id "
        f"WHERE p.id=?", (post_id,)).fetchone()
    out = sm._row_to_dict(row)
    out["attachments"] = _attachments(out.get("attachments"))
    # Живое обновление для остальных участников: раньше в форуме его не было,
    # и ответы приходили только после перезагрузки темы.
    await _notify_forum(thread["group_id"], thread_id, post_id, profile_id,
                       out.get("display_name") or profile_id, body)
    return JSONResponse(out)


async def _notify_forum(group_id, thread_id, post_id, author_id, author_name, body):
    """Сообщить участникам группы о новом ответе в теме."""
    try:
        await sm.manager.broadcast_to_group(group_id, {
            "type": "forum_post",
            "group_id": group_id,
            "thread_id": thread_id,
            "post_id": post_id,
            "author_id": author_id,
            "author_name": author_name,
            "content": (body or "")[:120],
        }, exclude_profile_id=author_id)
    except Exception:
        swallow.note('social_group_forum._notify_forum:L546')
        pass


@router.put("/forum/posts/{post_id}")
def update_post(post_id: str, request: Request, data: dict = Body(...)):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    row = conn.execute("SELECT * FROM social_forum_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    if row["author_id"] != profile_id:
        raise HTTPException(403, "Можно править только своё сообщение")
    body = _clean(data.get("content"), MAX_POST_CHARS)
    conn.execute(
        "UPDATE social_forum_posts SET content=?, edited_at=? WHERE id=?",
        (body, _now(), post_id))
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/forum/posts/{post_id}")
def delete_post(post_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    row = conn.execute("SELECT * FROM social_forum_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    role = _require_member(conn, row["group_id"], profile_id)
    if not _can_delete({"author_id": row["author_id"]}, profile_id, role):
        raise HTTPException(403, "Недостаточно прав")

    thread_id = row["thread_id"]
    conn.execute("DELETE FROM social_forum_posts WHERE id=?", (post_id,))
    if row["is_first"]:
        # Тело темы удалить нельзя: без него тема бессмысленна. На его
        # место ставим заглушку с тем же is_first, иначе тема осталась бы
        # вообще без первого сообщения, а счётчик ответов не сошёлся бы.
        conn.execute(
            "INSERT INTO social_forum_posts (id, thread_id, group_id, author_id, content, created_at, is_first) "
            "VALUES (?,?,?,?,?,?,1)",
            (uuid.uuid4().hex[:12], thread_id, row["group_id"], row["author_id"],
             "[Сообщение удалено автором]", row["created_at"]))
        # Счётчик не трогаем: заглушка заменила тело телы один в один.
    else:
        conn.execute(
            "UPDATE social_forum_threads SET posts_count=MAX(0,posts_count-1) WHERE id=?",
            (thread_id,))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/forum/posts/{post_id}/like")
def toggle_like(post_id: str, request: Request):
    profile_id = sm._get_active_profile(request)
    conn = sm._get_db()
    row = conn.execute("SELECT * FROM social_forum_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    _require_member(conn, row["group_id"], profile_id)

    existing = conn.execute(
        "SELECT 1 FROM social_forum_post_likes WHERE post_id=? AND profile_id=?",
        (post_id, profile_id)).fetchone()
    if existing:
        conn.execute(
            "DELETE FROM social_forum_post_likes WHERE post_id=? AND profile_id=?",
            (post_id, profile_id))
        conn.execute(
            "UPDATE social_forum_posts SET likes_count=MAX(0,likes_count-1) WHERE id=?",
            (post_id,))
        liked = False
    else:
        conn.execute(
            "INSERT INTO social_forum_post_likes (post_id, profile_id, created_at) VALUES (?,?,?)",
            (post_id, profile_id, _now()))
        conn.execute(
            "UPDATE social_forum_posts SET likes_count=likes_count+1 WHERE id=?", (post_id,))
        liked = True
    conn.commit()
    count = conn.execute(
        "SELECT likes_count FROM social_forum_posts WHERE id=?", (post_id,)).fetchone()[0]
    return JSONResponse({"liked": liked, "likes_count": count})
