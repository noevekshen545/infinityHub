"""REST API ботов мессенджера.

Отдельный роутер с тем же префиксом /api/social, что и social_module:
клиенту не нужно знать, в каком модуле живёт эндпоинт. Роутер подключается
в social_module через include_router в самом конце модуля.

Все операции изменения проверяют owner_id: чужим ботом нельзя управлять,
публичные боты (is_public=1) видны всем, но редактируются только владельцем.
"""
import json
import re
import time
import uuid

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import social_module as sm
import social_bot_engine as engine
import social_bot_factory
import swallow

router = APIRouter(prefix="/api/social", tags=["social-bots"])

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_]{1,29}$")
_VALID_TRIGGERS = ("mention", "keyword", "always")
_KINDS = ("dm", "group", "server")


def _profile(request: Request) -> str:
    return sm._get_active_profile(request)


def _clean_username(raw: str) -> str:
    name = (raw or "").strip().lstrip("@")
    if not _USERNAME_RE.match(name):
        raise HTTPException(400, "Username: 2-30 символов, латиница, цифры и _")
    return name


def _triggers_of(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except Exception:
            swallow.note('social_bot_api._triggers_of:L45')
            raw = []
    if not isinstance(raw, list):
        raw = []
    out = [t for t in (str(x).strip().lower() for x in raw) if t in _VALID_TRIGGERS]
    # Без mention бот не отвечает ни на что - это опечатка в UI, а не замысел.
    return out or ["mention"]


def _clean_keywords(raw) -> str:
    return ",".join(k.strip()[:32] for k in (raw or "").split(",") if k.strip())[:500]


def _row_out(row):
    d = dict(row)
    # token_hash бесполезен клиенту, а secret — действующий вебхук-секрет.
    # Раньше их отдавал даже /bots/{id} без проверки владельца.
    d.pop("token_hash", None)
    d.pop("secret", None)
    try:
        d["triggers"] = json.loads(d.get("triggers") or "[]")
    except Exception:
        swallow.note('social_bot_api._row_out:L67')
        d["triggers"] = ["mention"]
    d["is_enabled"] = bool(d.get("is_enabled"))
    d["is_public"] = bool(d.get("is_public"))
    return d


def _get_bot(bot_id: str):
    return sm._get_db().execute("SELECT * FROM social_bots WHERE id=?", (bot_id,)).fetchone()


def _owned(bot_id: str, profile_id: str):
    row = _get_bot(bot_id)
    if not row:
        raise HTTPException(404, "Бот не найден")
    if row["owner_id"] != profile_id:
        raise HTTPException(403, "Это не ваш бот")
    return row


def _known_models() -> list:
    """Список моделей из настроек - для выпадающего списка в UI."""
    try:
        import agent_module
        cfg = agent_module.get_llm_config() or {}
        names = list(cfg.get("available_models") or [])
        if cfg.get("model"):
            names.insert(0, cfg["model"])
        seen, out = set(), []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out
    except Exception:
        swallow.note('social_bot_api._known_models:L102')
        return []


def _can_use_chat(conn, kind: str, chat_id: str, profile_id: str) -> bool:
    """Членство в чате: иначе бота можно привязать к чужому диалогу и читать его."""
    if kind == "server":
        return bool(sm._server_role(conn, chat_id, profile_id))
    table = "social_conversation_members" if kind == "dm" else "social_group_members"
    col = "conversation_id" if kind == "dm" else "group_id"
    return bool(conn.execute(
        f"SELECT 1 FROM {table} WHERE {col}=? AND profile_id=?", (chat_id, profile_id)).fetchone())


# --- BotFather: выдача и перевыпуск токена ---
# Отдельный блок от CRUD ботов: здесь создаётся именно внешний бот
# (kind='external') и выдаётся токен для Bot API. Токен показывается один
# раз — в базе лежит только sha256, поэтому восстановить его нельзя,
# только перевыпустить.


def _row_out_public(row):
    """Запись бота для интерфейса: без секретов, но со статусом токена."""
    out = _row_out(row)
    out["has_token"] = bool(row["token_hash"]) if "token_hash" in row.keys() else False
    out["token_prefix"] = row["token_prefix"] or "" if "token_prefix" in row.keys() else ""
    out["last_seen_at"] = row["last_seen_at"] if "last_seen_at" in row.keys() else 0
    out["last_error"] = row["last_error"] if "last_error" in row.keys() else ""
    out["transport"] = row["transport"] if "transport" in row.keys() else "long_poll"
    return out


@router.post("/botfather/bots")
def botfather_create(request: Request, data: dict = Body(...)):
    """Создать внешнего бота и вернуть токен. Токен в ответе — единственный
    раз, когда он доступен: хранится только хэш."""
    profile_id = _profile(request)
    if not sm._check_rate_limit(f"botfather:{profile_id}", 10, 3600):
        raise HTTPException(429, "Слишком много ботов создано. Попробуйте позже.")
    try:
        row, token = social_bot_factory.create_bot(
            profile_id,
            data.get("username"),
            data.get("display_name") or "",
            data.get("bio") or "",
            data.get("description") or "",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = _row_out_public(row)
    out["token"] = token
    return JSONResponse({"ok": True, "bot": out})


@router.post("/botfather/bots/{bot_id}/token")
def botfather_reissue(bot_id: str, request: Request):
    """Перевыпустить токен: вернуть новый, старый перестаёт работать."""
    profile_id = _profile(request)
    row = _owned(bot_id, profile_id)
    token_id = bot_id.replace("bot:", "", 1)
    if not row["token_prefix"]:
        raise HTTPException(400, "У этого бота не был выдан токен")
    if not sm._check_rate_limit(f"botfather:{profile_id}", 10, 3600):
        raise HTTPException(429, "Слишком много перевыпусков. Попробуйте позже.")
    token = social_bot_factory.reissue(token_id)
    return JSONResponse({"ok": True, "token": token})


@router.get("/botfather/bots")
def botfather_list(request: Request):
    """Внешние боты владельца — для списка на странице BotFather."""
    profile_id = _profile(request)
    rows = sm._get_db().execute(
        "SELECT * FROM social_bots WHERE owner_id=? AND kind='external' "
        "ORDER BY created_at DESC", (profile_id,)).fetchall()
    return JSONResponse({"bots": [_row_out_public(r) for r in rows]})


# --- Список и просмотр ---

@router.get("/bots")
def list_bots(request: Request):
    profile_id = _profile(request)
    conn = sm._get_db()
    mine = conn.execute(
        "SELECT * FROM social_bots WHERE owner_id=? ORDER BY created_at DESC", (profile_id,)
    ).fetchall()
    # Публичные боты, уже привязанные к диалогу, где я состою: их нельзя
    # редактировать, но они пишут в чат и должны быть видны в списке.
    shared = conn.execute(
        "SELECT DISTINCT b.* FROM social_bots b "
        "JOIN social_bot_chats c ON c.bot_id=b.id "
        "WHERE b.is_public=1 AND c.enabled=1 AND c.chat_kind IN ('dm','group') AND c.chat_id IN ("
        "  SELECT conversation_id FROM social_conversation_members WHERE profile_id=?"
        ") ORDER BY b.created_at DESC", (profile_id,)
    ).fetchall()
    rows = list(mine)
    have = {r["id"] for r in rows}
    for r in shared:
        if r["id"] not in have:
            rows.append(r)
    return JSONResponse({"bots": [_row_out(r) for r in rows], "models": _known_models()})


@router.get("/bots/{bot_id}")
def get_bot(bot_id: str, request: Request):
    row = _get_bot(bot_id)
    if not row:
        raise HTTPException(404, "Бот не найден")
    profile_id = _profile(request)
    if row["owner_id"] != profile_id and not row["is_public"]:
        raise HTTPException(403, "Это не ваш бот")
    chats = sm._get_db().execute(
        "SELECT chat_kind, chat_id FROM social_bot_chats WHERE bot_id=?", (bot_id,)
    ).fetchall()
    out = _row_out(row)
    out["chats"] = [{"chat_kind": c["chat_kind"], "chat_id": c["chat_id"]} for c in chats]
    return JSONResponse(out)


@router.get("/chats/{chat_kind}/{chat_id}/bots")
def list_chat_bots(chat_kind: str, chat_id: str, request: Request):
    """Боты, уже отвечающие в этом чате - для панели в шапке диалога."""
    if chat_kind not in _KINDS:
        raise HTTPException(400, "Неизвестный тип чата")
    conn = sm._get_db()
    rows = conn.execute(
        "SELECT b.* FROM social_bots b JOIN social_bot_chats c ON c.bot_id=b.id "
        "WHERE c.chat_kind=? AND c.chat_id=? AND c.enabled=1 AND b.is_enabled=1",
        (chat_kind, chat_id)).fetchall()
    return JSONResponse({"bots": [_row_out(r) for r in rows]})
# --- Создание / изменение / удаление ---

@router.post("/bots")
def create_bot(request: Request, data: dict = Body(...)):
    profile_id = _profile(request)
    if not sm._check_rate_limit(f"bot:{profile_id}", 10, 3600):
        raise HTTPException(429, "Слишком много ботов создано. Попробуйте позже.")
    conn = sm._get_db()
    username = _clean_username(data.get("username"))
    if conn.execute("SELECT 1 FROM social_bots WHERE username=?", (username,)).fetchone():
        raise HTTPException(400, "Этот @username уже занят")
    display_name = (data.get("display_name") or username).strip()[:64]
    bio = (data.get("bio") or "").strip()[:500]
    triggers = _triggers_of(data.get("triggers"))
    keywords = _clean_keywords(data.get("keywords"))
    if "keyword" in triggers and not keywords:
        raise HTTPException(400, "Для триггера по ключевым словам нужны сами слова")
    try:
        temperature = max(0.0, min(float(data.get("temperature") or 0.7), 2.0))
        max_context = max(1, min(int(data.get("max_context") or 20), 100))
    except (TypeError, ValueError):
        raise HTTPException(400, "Температура и глубина контекста должны быть числами")
    now = time.time()
    bot_id = "bot:" + str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_bots (id, owner_id, username, display_name, bio, system_prompt, "
        "model, temperature, max_context, is_enabled, is_public, triggers, keywords, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bot_id, profile_id, username, display_name, bio,
         (data.get("system_prompt") or "").strip()[:4000],
         (data.get("model") or "").strip()[:120], temperature, max_context,
         0 if data.get("is_enabled") is False else 1,
         1 if data.get("is_public") else 0,
         json.dumps(triggers, ensure_ascii=False), keywords, now, now))
    # Запись в social_users обязательна: на sender_id ссылаются все таблицы
    # сообщений, и без неё JOIN social_users в выборках потерял бы бота.
    conn.execute(
        "INSERT OR IGNORE INTO social_users (profile_id, display_name, bio, joined_at, is_bot) "
        "VALUES (?,?,?,?,1)", (bot_id, display_name, bio, now))
    conn.commit()
    return JSONResponse({"ok": True, "bot": _row_out(_get_bot(bot_id))})


@router.put("/bots/{bot_id}")
def update_bot(bot_id: str, request: Request, data: dict = Body(...)):
    profile_id = _profile(request)
    row = _owned(bot_id, profile_id)
    conn = sm._get_db()
    fields, values = [], []

    def put(col, val):
        fields.append(f"{col}=?")
        values.append(val)

    if "username" in data:
        username = _clean_username(data["username"])
        if conn.execute("SELECT 1 FROM social_bots WHERE username=? AND id!=?",
                        (username, bot_id)).fetchone():
            raise HTTPException(400, "Этот @username уже занят")
        put("username", username)
    if "display_name" in data:
        name = (data.get("display_name") or "").strip()[:64]
        if not name:
            raise HTTPException(400, "Имя бота не может быть пустым")
        put("display_name", name)
        conn.execute("UPDATE social_users SET display_name=? WHERE profile_id=?", (name, bot_id))
    if "bio" in data:
        put("bio", (data.get("bio") or "").strip()[:500])
        conn.execute("UPDATE social_users SET bio=? WHERE profile_id=?",
                     ((data.get("bio") or "").strip()[:500], bot_id))
    for col, limit in (("system_prompt", 4000), ("model", 120)):
        if col in data:
            put(col, (data.get(col) or "").strip()[:limit])
    if "keywords" in data:
        put("keywords", _clean_keywords(data.get("keywords")))
    if "triggers" in data:
        trg = _triggers_of(data.get("triggers"))
        kw = _clean_keywords(data.get("keywords")) if "keywords" in data else (row["keywords"] or "")
        if "keyword" in trg and not kw.strip():
            raise HTTPException(400, "Для триггера по ключевым словам нужны сами слова")
        put("triggers", json.dumps(trg, ensure_ascii=False))
    if "temperature" in data:
        try:
            put("temperature", max(0.0, min(float(data["temperature"]), 2.0)))
        except (TypeError, ValueError):
            raise HTTPException(400, "Температура должна быть числом")
    if "max_context" in data:
        try:
            put("max_context", max(1, min(int(data["max_context"]), 100)))
        except (TypeError, ValueError):
            raise HTTPException(400, "Глубина контекста должна быть целым числом")
    if "is_enabled" in data:
        put("is_enabled", 1 if data["is_enabled"] else 0)
    if "is_public" in data:
        put("is_public", 1 if data["is_public"] else 0)
    if not fields:
        return JSONResponse({"ok": True, "bot": _row_out(row)})
    put("updated_at", time.time())
    values.append(bot_id)
    conn.execute(f"UPDATE social_bots SET {','.join(fields)} WHERE id=?", values)
    conn.commit()
    return JSONResponse({"ok": True, "bot": _row_out(_get_bot(bot_id))})


@router.delete("/bots/{bot_id}")
def delete_bot(bot_id: str, request: Request):
    profile_id = _profile(request)
    _owned(bot_id, profile_id)
    conn = sm._get_db()
    conn.execute("DELETE FROM social_bot_chats WHERE bot_id=?", (bot_id,))
    conn.execute("DELETE FROM social_bot_log WHERE bot_id=?", (bot_id,))
    conn.execute("DELETE FROM social_bots WHERE id=?", (bot_id,))
    # Запись в social_users не удаляем: на неё ссылаются уже отправленные
    # сообщения бота, и удаление сломало бы их отображение. Сбрасываем
    # только флаг is_bot — пользователь увидит «удалённого бота» как обычный
    # профиль, а его сообщения останутся читаемыми.
    conn.execute("UPDATE social_users SET is_bot=0 WHERE profile_id=?", (bot_id,))
    conn.commit()
    return JSONResponse({"ok": True})


# --- Аватар ---

@router.post("/bots/{bot_id}/avatar")
def upload_bot_avatar(bot_id: str, request: Request, file: UploadFile = File(...)):
    profile_id = _profile(request)
    _owned(bot_id, profile_id)
    if not sm._check_rate_limit(f"botavatar:{profile_id}", 10, 3600):
        raise HTTPException(429, "Слишком много загрузок. Попробуйте позже.")
    name = file.filename or "a.jpg"
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ".jpg"
    if ext not in sm._ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат. Разрешены: jpg, png, gif, webp")
    filename = f"bot_{uuid.uuid4().hex[:16]}{ext}"
    sm._save_upload(file, sm.os.path.join(sm.AVATARS_DIR, filename),
                    sm._MAX_IMAGE_BYTES, image_only=True)
    path = f"/api/social/file/avatars/{filename}"
    conn = sm._get_db()
    conn.execute("UPDATE social_bots SET avatar_path=?, updated_at=? WHERE id=?",
                 (path, time.time(), bot_id))
    conn.execute("UPDATE social_users SET avatar_path=? WHERE profile_id=?", (path, bot_id))
    conn.commit()
    return JSONResponse({"ok": True, "path": path})


# --- Привязка к чатам ---

@router.post("/bots/{bot_id}/chats")
def attach_bot(bot_id: str, request: Request, data: dict = Body(...)):
    profile_id = _profile(request)
    row = _owned(bot_id, profile_id)
    kind = (data.get("chat_kind") or "").strip().lower()
    chat_id = (data.get("chat_id") or "").strip()
    if kind not in _KINDS or not chat_id:
        raise HTTPException(400, "Нужны chat_kind (dm|group|server) и chat_id")
    conn = sm._get_db()
    if not _can_use_chat(conn, kind, chat_id, profile_id):
        raise HTTPException(403, "Вы не участник чата")
    conn.execute(
        "INSERT OR REPLACE INTO social_bot_chats (bot_id, chat_kind, chat_id, enabled, added_at) "
        "VALUES (?,?,?,1,?)", (bot_id, kind, chat_id, time.time()))
    conn.commit()
    return JSONResponse({"ok": True, "username": row["username"]})


@router.delete("/bots/{bot_id}/chats/{chat_kind}/{chat_id}")
def detach_bot(bot_id: str, chat_kind: str, chat_id: str, request: Request):
    profile_id = _profile(request)
    _owned(bot_id, profile_id)
    sm._get_db().execute(
        "DELETE FROM social_bot_chats WHERE bot_id=? AND chat_kind=? AND chat_id=?",
        (bot_id, chat_kind, chat_id))
    sm._get_db().commit()
    return JSONResponse({"ok": True})


# --- Тест генерации без отправки в чат ---

@router.post("/bots/{bot_id}/test")
async def test_bot(bot_id: str, request: Request, data: dict = Body(...)):
    profile_id = _profile(request)
    row = _owned(bot_id, profile_id)
    if not sm._check_rate_limit(f"bottest:{profile_id}", 20, 300):
        raise HTTPException(429, "Слишком много тестов. Подождите пару минут.")
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Введите текст для проверки")
    answer = await engine.BotEngine()._generate(row, [{"role": "user", "content": text}])
    return JSONResponse({"ok": True, "answer": answer,
                         "warning": None if answer else "Модель не ответила"})
