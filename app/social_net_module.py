"""Кросс-серверная соцсеть через координационный релей.

Профили (каталог), ЛС и серверы (группы) между нодами сети координации:
  * каталог публичных профилей нод — через announce (profiles) + coordination.json;
  * ЛС — канал "social-dm" (targeted);
  * серверы — канал "social-server:<id>" (broadcast после подтверждения владельцем);
  * приглашение сервера — канал "social-invite" (targeted).

Каналы координации обрабатываются в poll-потоках; рассылка локальным
WS-клиентам соцсети — через asyncio-мост (set_loop + ws_out_task).
"""

import asyncio
import json
import logging
import queue
import threading
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import swallow

logger = logging.getLogger("infinity")

router = APIRouter(prefix="/api/social/net", tags=["social-net"])

_LOCK = threading.RLock()
_SUBS_READY = False

_EVENT_LOOP = None
_OUT_Q = None

MAX_PROFILES = 25
MAX_SERVER_MSGS = 300


# ---------------------------------------------------------------- helpers

def _get_db():
    import social_module
    return social_module._get_db()


def _now():
    import social_module
    return social_module._now()


def _active_profile():
    try:
        import profiles_module
        active = profiles_module.get_active_profile()
        return active.get("id") or "" if active else ""
    except Exception:
        swallow.note('social_net_module._active_profile:L56')
        return ""


def _local_display_name(profile_id):
    import social_module
    try:
        return social_module._get_profile_name(profile_id) or profile_id
    except Exception:
        swallow.note('social_net_module._local_display_name:L65')
        return profile_id or "Пользователь"


def _ensure_social_user(profile_id, display_name=""):
    import social_module
    try:
        social_module._ensure_user(profile_id, display_name or "Пользователь")
    except Exception as e:
        logger.error(f"social-net: ensure user: {e}")


def _remote_uid(node, profile_id):
    return f"remote:{node}:{profile_id}"


def _split_remote(uid):
    parts = uid.split(":")
    if len(parts) >= 3 and parts[0] == "remote":
        return parts[1], ":".join(parts[2:])
    return None, None


# ------------------------------------------------------------ asyncio bridge

def set_loop(loop):
    global _EVENT_LOOP, _OUT_Q
    _EVENT_LOOP = loop
    _OUT_Q = asyncio.Queue()


async def ws_out_task():
    """Рассылка событий локальным WS-клиентам соцсети (в event loop)."""
    while True:
        try:
            profile_id, payload = await _OUT_Q.get()
            import social_module
            await social_module.manager.send_to_profile(profile_id, payload)
        except Exception:
            swallow.note('social_net_module.ws_out_task:L104')
            await asyncio.sleep(1)


def _push_ws(profile_id, payload):
    try:
        q = _OUT_Q
        loop = _EVENT_LOOP
        if q is not None and loop is not None:
            loop.call_soon_threadsafe(q.put_nowait, (profile_id, payload))
    except Exception:
        swallow.note('social_net_module._push_ws:L115')
        pass


# ---------------------------------------------------------------- network

def _coordination():
    import coordination_module
    return coordination_module


def _node_id():
    import coordination_module
    return coordination_module.node_id()


def _enabled():
    import coordination_module
    try:
        return coordination_module.enabled()
    except Exception:
        swallow.note('social_net_module._enabled:L136')
        return False


def init_networking():
    """Подписать каналы координации для соцсети (идемпотентно)."""
    global _SUBS_READY
    try:
        cm = _coordination()
        cm.subscribe("social-dm", _social_dispatch)
        cm.subscribe("social-invite", _social_dispatch)
        cm.subscribe("social-server:", _social_dispatch)
        cm.subscribe_channel("social-dm")
        cm.subscribe_channel("social-invite")
        for sid in _known_server_ids():
            cm.subscribe_channel("social-server:" + sid)
        cm.add_announce_hook(_announce_profiles)
        _SUBS_READY = True
        logger.info("social-net: каналы координации подписаны")
    except Exception as e:
        logger.error(f"social-net: init: {e}")


def _known_server_ids():
    ids = []
    try:
        conn = _get_db()
        rows = conn.execute("SELECT id FROM social_net_servers").fetchall()
        ids = [r["id"] for r in rows]
    except Exception:
        swallow.note('social_net_module._known_server_ids:L166')
        pass
    return ids


def _announce_profiles():
    """Публичные профили этой ноды для каталога сети."""
    try:
        import social_module
        conn = social_module._get_db()
        rows = conn.execute(
            "SELECT profile_id, display_name, bio, status, avatar_path, last_seen "
            "FROM social_users WHERE profile_privacy=0 ORDER BY last_seen DESC LIMIT ?",
            (MAX_PROFILES,),
        ).fetchall()
        profiles = []
        for r in rows:
            profiles.append({
                "profile_id": r["profile_id"],
                "display_name": (r["display_name"] or r["profile_id"])[:200],
                "bio": (r["bio"] or "")[:300],
                "status": (r["status"] or "")[:200],
                "avatar": (r["avatar_path"] or "")[:300],
                "last_seen": r["last_seen"] or 0,
            })
        return {"profiles": profiles}
    except Exception:
        swallow.note('social_net_module._announce_profiles:L193')
        return {}


# ------------------------------------------------------------ dispatcher

def _social_dispatch(channel, payload, from_node):
    try:
        if not _SUBS_READY:
            init_networking()
        if from_node and from_node == _node_id():
            return
        if channel == "social-dm":
            _handle_dm(payload)
        elif channel == "social-invite":
            _handle_invite(payload)
        elif channel.startswith("social-server:"):
            sid = channel[len("social-server:"):]
            _handle_server_event(sid, payload, from_node)
    except Exception as e:
        logger.error(f"social-net: dispatch: {e}")


# ------------------------------------------------------------ catalog

def _collect_catalog():
    """Собрать публичные профили всех нод сети (из кэша реестра)."""
    result = []
    try:
        cm = _coordination()
        peers = cm.get_peers()
        if isinstance(peers, dict):
            peers = [dict(v, node_id=k) for k, v in peers.items()]
        for info in peers:
            profiles = (info or {}).get("profiles") or []
            if not profiles:
                continue
            result.append({
                "node": info.get("node_id") or "",
                "node_name": info.get("name") or info.get("node_id") or "",
                "profiles": profiles,
            })
    except Exception as e:
        logger.error(f"social-net: catalog: {e}")
    return result


@router.get("/catalog")
def api_catalog():
    return JSONResponse({"ok": True, "nodes": _collect_catalog()})


@router.get("/remote-profile")
def api_remote_profile(profile_id: str):
    """Локальный публичный профиль (для RPC других нод)."""
    try:
        import social_module
        conn = social_module._get_db()
        r = conn.execute(
            "SELECT profile_id, display_name, bio, status, avatar_path, last_seen "
            "FROM social_users WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if not r:
            return JSONResponse({"ok": False, "error": "Профиль не найден"}, 404)
        return JSONResponse({"ok": True, "profile": {
            "profile_id": r["profile_id"],
            "display_name": r["display_name"] or r["profile_id"],
            "bio": r["bio"] or "",
            "status": r["status"] or "",
            "avatar": r["avatar_path"] or "",
            "last_seen": r["last_seen"] or 0,
        }})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@router.get("/profile")
def api_profile(node: str, profile_id: str):
    """Профиль пользователя на другой ноде (RPC через релей)."""
    try:
        cm = _coordination()
        res = cm.remote_request(node, "GET", "/api/social/net/remote-profile",
                                params={"profile_id": profile_id})
        if res and res.get("ok") and res.get("data", {}).get("ok"):
            return JSONResponse({"ok": True, "profile": res["data"]["profile"],
                                 "node": node})
        return JSONResponse({"ok": False, "error": "Не удалось получить профиль"}, 404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


# ------------------------------------------------------------ DMs

@router.post("/dm")
async def api_dm(request: Request):
    body = await request.json()
    to_node = str(body.get("to_node") or "")
    to_profile = str(body.get("to_profile") or "")
    text = (body.get("text") or "").strip()
    if not to_node or not to_profile or not text:
        return JSONResponse({"ok": False, "error": "Не хватает параметров"}, 400)
    if len(text) > 4000:
        return JSONResponse({"ok": False, "error": "Сообщение слишком длинное"}, 400)
    if not _enabled():
        return JSONResponse({"ok": False, "error": "Координация выключена"}, 400)
    if to_node == _node_id():
        return JSONResponse({"ok": False, "error": "Это локальный профиль"}, 400)
    from_profile = _active_profile()
    if not from_profile:
        return JSONResponse({"ok": False, "error": "Нет активного профиля"}, 400)
    payload = {
        "dm_id": uuid.uuid4().hex[:12],
        "from_node": _node_id(),
        "from_profile": from_profile,
        "from_name": _local_display_name(from_profile),
        "to_profile": to_profile,
        "text": text,
        "ts": time.time(),
    }
    ok = _coordination().send_event(to_node, "social-dm", payload)
    if not ok:
        return JSONResponse({"ok": False, "error": "Нода недоступна"}, 502)
    remote = _remote_uid(to_node, to_profile)
    try:
        conn = _get_db()
        now = _now()
        conv_id = _find_or_create_dm(conn, from_profile, remote, now)
        conn.execute(
            "INSERT INTO social_messages (id, conversation_id, sender_id, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (payload["dm_id"], conv_id, from_profile, text, now),
        )
        conn.execute("UPDATE social_conversations SET updated_at=? WHERE id=?",
                     (now, conv_id))
        conn.commit()
    except Exception as e:
        logger.error(f"social-net: local dm save: {e}")
    return JSONResponse({"ok": True, "dm_id": payload["dm_id"]})


def _handle_dm(payload):
    """Входящее ЛС с другой ноды."""
    try:
        from_node = str(payload.get("from_node") or "")
        from_profile = str(payload.get("from_profile") or "")
        to_profile = str(payload.get("to_profile") or "")
        text = (payload.get("text") or "").strip()
        if not from_node or not from_profile or not text:
            return
        if not to_profile:
            to_profile = _active_profile()
        conn = _get_db()
        exists = conn.execute(
            "SELECT 1 FROM social_users WHERE profile_id=?", (to_profile,)
        ).fetchone()
        if not exists:
            to_profile = _active_profile()
        if not to_profile:
            return
        remote = _remote_uid(from_node, from_profile)
        _ensure_social_user(remote, payload.get("from_name") or "Удалённый пользователь")
        now = _now()
        conv_id = _find_or_create_dm(conn, to_profile, remote, now)
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO social_messages (id, conversation_id, sender_id, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (msg_id, conv_id, remote, text, now),
        )
        conn.execute(
            "UPDATE social_conversations SET updated_at=? WHERE id=?",
            (now, conv_id),
        )
        conn.commit()
        try:
            import social_module
            social_module._create_notification(conn, to_profile, "dm", remote,
                                               target_type="conversation", target_id=conv_id)
            conn.commit()
        except Exception:
            swallow.note('social_net_module._handle_dm:L373')
            pass
        _push_ws(to_profile, {
            "type": "new_dm",
            "conv_id": conv_id,
            "message": {
                "id": msg_id,
                "conversation_id": conv_id,
                "sender_id": remote,
                "content": text,
                "created_at": now,
            },
            "sender": {"profile_id": remote, "display_name": payload.get("from_name") or "Удалённый пользователь"},
        })
        logger.info(f"social-net: ЛС от ноды {from_node} ({from_profile}) -> {to_profile}")
    except Exception as e:
        logger.error(f"social-net: dm: {e}")


def _find_or_create_dm(conn, local_id, remote_id, now):
    row = conn.execute(
        "SELECT c.id FROM social_conversations c "
        "JOIN social_conversation_members m1 ON c.id=m1.conversation_id AND m1.profile_id=? "
        "JOIN social_conversation_members m2 ON c.id=m2.conversation_id AND m2.profile_id=? "
        "WHERE c.is_group=0 AND (SELECT COUNT(*) FROM social_conversation_members WHERE conversation_id=c.id)=2",
        (local_id, remote_id),
    ).fetchone()
    if row:
        return row["id"]
    conv_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_conversations (id, title, created_at, is_group) VALUES (?,?,?,0)",
        (conv_id, "", now),
    )
    for pid in (local_id, remote_id):
        conn.execute(
            "INSERT INTO social_conversation_members (conversation_id, profile_id, joined_at) VALUES (?,?,?)",
            (conv_id, pid, now),
        )
    return conv_id


@router.get("/conversations")
def api_net_conversations():
    """Локальные диалоги с удалёнными участниками."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT c.id, c.updated_at, u.display_name, u.profile_id, u.last_seen, "
            "(SELECT content FROM social_messages m WHERE m.conversation_id=c.id ORDER BY m.created_at DESC LIMIT 1) as last_text, "
            "(SELECT created_at FROM social_messages m WHERE m.conversation_id=c.id ORDER BY m.created_at DESC LIMIT 1) as last_ts "
            "FROM social_conversations c "
            "JOIN social_conversation_members cm ON cm.conversation_id=c.id AND cm.profile_id!=? "
            "JOIN social_users u ON u.profile_id=cm.profile_id "
            "WHERE u.profile_id LIKE 'remote:%' ORDER BY c.updated_at DESC",
            (_active_profile(),),
        ).fetchall()
        items = []
        for r in rows:
            node, rpid = _split_remote(r["profile_id"])
            items.append({
                "id": r["id"],
                "updated_at": r["updated_at"],
                "remote": {"node": node or "", "profile_id": rpid or ""},
                "display_name": r["display_name"],
                "last_text": r["last_text"] or "",
                "last_ts": r["last_ts"] or 0,
            })
        return JSONResponse({"ok": True, "conversations": items})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@router.get("/conversations/{conv_id}/messages")
def api_net_messages(conv_id: str, limit: int = 50):
    try:
        import social_module
        conn = _get_db()
        rows = conn.execute(
            "SELECT m.id, m.sender_id, m.content, m.created_at, u.display_name "
            "FROM social_messages m JOIN social_users u ON u.profile_id=m.sender_id "
            "WHERE m.conversation_id=? ORDER BY m.created_at ASC LIMIT ?",
            (conv_id, min(max(limit, 1), 200)),
        ).fetchall()
        return JSONResponse({"ok": True, "messages": [
            {"id": r["id"], "sender_id": r["sender_id"], "content": r["content"],
             "created_at": r["created_at"], "display_name": r["display_name"]} for r in rows
        ]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


# ------------------------------------------------------------ servers

def _server_payload(conn, server_id, owner_id):
    """Собрать описание локального сервера для приглашения ноды."""
    srv = conn.execute("SELECT * FROM social_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        return None
    channels = []
    for ch in conn.execute(
        "SELECT id, name, type FROM social_server_channels WHERE server_id=? ORDER BY created_at",
        (server_id,),
    ).fetchall():
        channels.append({"id": ch["id"], "name": ch["name"], "type": ch["type"]})
    members = {}
    for m in conn.execute(
        "SELECT profile_id, role FROM social_server_members WHERE server_id=?",
        (server_id,),
    ).fetchall():
        members[m["profile_id"]] = {"name": _local_display_name(m["profile_id"]),
                                    "role": m["role"]}
    return {
        "type": "server",
        "server_id": server_id,
        "name": srv["name"],
        "owner_node": _node_id(),
        "owner_name": _local_display_name(owner_id),
        "channels": channels,
        "members": members,
    }


@router.post("/invite-server")
async def api_invite_server(request: Request):
    body = await request.json()
    server_id = str(body.get("server_id") or "")
    node_id = str(body.get("node_id") or "")
    if not server_id or not node_id:
        return JSONResponse({"ok": False, "error": "Не хватает параметров"}, 400)
    if not _enabled():
        return JSONResponse({"ok": False, "error": "Координация выключена"}, 400)
    try:
        import social_module
        profile_id = social_module._get_active_profile(request)
        conn = social_module._get_db()
        role = social_module._server_role(conn, server_id, profile_id)
        if role != "owner":
            return JSONResponse({"ok": False, "error": "Только владелец сервера может приглашать ноду"}, 403)
        payload = _server_payload(conn, server_id, profile_id)
        if not payload:
            return JSONResponse({"ok": False, "error": "Сервер не найден"}, 404)
        ok = _coordination().send_event(node_id, "social-invite", payload)
        if not ok:
            return JSONResponse({"ok": False, "error": "Нода недоступна"}, 502)
        _coordination().subscribe_channel("social-server:" + server_id)
        return JSONResponse({"ok": True, "server_id": server_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


def _handle_invite(payload):
    """Приглашение ноды в сервер: сохранить mirror и подписаться на канал."""
    try:
        if payload.get("type") != "server":
            return
        server_id = str(payload.get("server_id") or "")
        if not server_id:
            return
        conn = _get_db()
        now = _now()
        exists = conn.execute(
            "SELECT id FROM social_net_servers WHERE id=?", (server_id,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO social_net_servers (id, owner_node, name, channels, members, messages, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (server_id, payload.get("owner_node") or "", payload.get("name") or server_id,
                 json.dumps(payload.get("channels") or [], ensure_ascii=False),
                 json.dumps(payload.get("members") or {}, ensure_ascii=False),
                 json.dumps({}, ensure_ascii=False), now),
            )
            conn.commit()
        else:
            conn.execute(
                "UPDATE social_net_servers SET owner_node=?, name=?, channels=?, members=?, updated_at=? WHERE id=?",
                (payload.get("owner_node") or "", payload.get("name") or server_id,
                 json.dumps(payload.get("channels") or [], ensure_ascii=False),
                 json.dumps(payload.get("members") or {}, ensure_ascii=False), now, server_id),
            )
            conn.commit()
        _coordination().subscribe_channel("social-server:" + server_id)
        logger.info(f"social-net: приглашение в сервер {server_id} от {payload.get('owner_node')}")
    except Exception as e:
        logger.error(f"social-net: invite: {e}")


def _handle_server_event(server_id, payload, from_node):
    """Событие сервера: владелец пишет в БД и публикует broadcast;
    гости обновляют mirror-кеш."""
    try:
        import social_module
        mtype = payload.get("type")
        logger.info(f"social-net: server event {server_id} type={mtype} from={from_node}")
        if mtype == "msg":
            channel_id = str(payload.get("channel_id") or "")
            text = (payload.get("text") or "").strip()
            if not channel_id:
                return
            if not text:
                text = (payload.get("msg") or {}).get("content") or ""
            conn = _get_db()
            is_owner = bool(conn.execute(
                "SELECT 1 FROM social_server_channels WHERE id=?", (channel_id,)
            ).fetchone())
            if is_owner:
                if from_node and from_node == _node_id():
                    return
                remote = _remote_uid(from_node, payload.get("from_profile") or "guest")
                _ensure_social_user(remote, payload.get("from_name") or "Удалённый")
                msg_id = str(uuid.uuid4())
                now = _now()
                conn.execute(
                    "INSERT INTO social_server_messages (id, channel_id, sender_id, content, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (msg_id, channel_id, remote, text, now),
                )
                conn.commit()
                msg = {"id": msg_id, "channel_id": channel_id, "sender_id": remote,
                       "content": text, "sticker": "", "attachment": "", "reply_to": "",
                       "edited_at": 0, "is_pinned": 0, "created_at": now}
                payload = {"type": "msg", "channel_id": channel_id, "msg": msg,
                           "from_node": from_node, "from_profile": payload.get("from_profile"),
                           "from_name": payload.get("from_name"), "msg_id": msg_id}
                _publish_server_event(server_id, payload)
                for pid in list(social_module.manager.server_subs.get(server_id, set())):
                    _push_ws(pid, {"type": "server_msg", "server_id": server_id,
                                   "channel_id": channel_id, "message": msg})
            else:
                if from_node == _node_id():
                    return
                m = payload.get("msg") or {}
                _update_mirror_msg(conn, server_id, m.get("channel_id") or channel_id, m, payload.get("msg_id") or "")
                for pid in list(social_module.manager.server_subs.get(server_id, set())):
                    _push_ws(pid, {"type": "server_msg", "server_id": server_id,
                                   "channel_id": m.get("channel_id") or channel_id, "message": m})
    except Exception as e:
        logger.error(f"social-net: server event: {e}")


def _update_mirror_msg(conn, server_id, channel_id, msg, msg_id):
    """Добавить сообщение в кеш mirror-сервера."""
    try:
        row = conn.execute("SELECT messages FROM social_net_servers WHERE id=?", (server_id,)).fetchone()
        data = {}
        if row:
            try:
                data = json.loads(row["messages"] or "{}")
            except Exception:
                swallow.note('social_net_module._update_mirror_msg:L623')
                data = {}
        ch_msgs = data.get(channel_id) or []
        if not any(m.get("id") == msg_id or m.get("id") == (msg or {}).get("id") for m in ch_msgs):
            ch_msgs.append(msg)
            data[channel_id] = ch_msgs[-MAX_SERVER_MSGS:]
            conn.execute("UPDATE social_net_servers SET messages=? WHERE id=?",
                         (json.dumps(data, ensure_ascii=False), server_id))
            conn.commit()
    except Exception as e:
        logger.error(f"social-net: mirror msg: {e}")


def _publish_server_event(server_id, payload):
    """Опубликовать событие сервера в канал (broadcast)."""
    try:
        _coordination().send_event("*", "social-server:" + server_id, payload)
    except Exception as e:
        logger.error(f"social-net: publish server event: {e}")


@router.post("/server-msg")
async def api_server_msg(request: Request):
    body = await request.json()
    server_id = str(body.get("server_id") or "")
    channel_id = str(body.get("channel_id") or "")
    text = (body.get("text") or "").strip()
    if not server_id or not channel_id or not text:
        return JSONResponse({"ok": False, "error": "Не хватает параметров"}, 400)
    if len(text) > 4000:
        return JSONResponse({"ok": False, "error": "Сообщение слишком длинное"}, 400)
    if not _enabled():
        return JSONResponse({"ok": False, "error": "Координация выключена"}, 400)
    conn = _get_db()
    row = conn.execute("SELECT * FROM social_net_servers WHERE id=?", (server_id,)).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "Сервер не известен этой ноде"}, 404)
    owner_node = row["owner_node"]
    if not owner_node or owner_node == _node_id():
        return JSONResponse({"ok": False, "error": "Сервер не удалённый"}, 400)
    payload = {
        "type": "msg",
        "channel_id": channel_id,
        "text": text,
        "from_node": _node_id(),
        "from_profile": _active_profile(),
        "from_name": _local_display_name(_active_profile()),
    }
    ok = _coordination().send_event(owner_node, "social-server:" + server_id, payload)
    if not ok:
        return JSONResponse({"ok": False, "error": "Нода-владелец недоступна"}, 502)
    return JSONResponse({"ok": True, "server_id": server_id, "channel_id": channel_id})


@router.get("/servers")
def api_net_servers():
    """Присоединённые удалённые серверы (mirror)."""
    try:
        conn = _get_db()
        rows = conn.execute("SELECT * FROM social_net_servers ORDER BY updated_at DESC").fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "name": r["name"],
                "owner_node": r["owner_node"],
                "channels": json.loads(r["channels"] or "[]"),
                "members": json.loads(r["members"] or "{}"),
                "updated_at": r["updated_at"],
            })
        return JSONResponse({"ok": True, "servers": items})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@router.get("/servers/{server_id}/messages")
def api_net_server_messages(server_id: str, channel_id: str = ""):
    try:
        conn = _get_db()
        row = conn.execute("SELECT messages FROM social_net_servers WHERE id=?", (server_id,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Сервер не найден"}, 404)
        data = json.loads(row["messages"] or "{}")
        if channel_id:
            data = {channel_id: data.get(channel_id) or []}
        return JSONResponse({"ok": True, "channels": data})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


def on_local_server_message(server_id, channel_id, msg, profile_id):
    """Хук из social_module: локальное сообщение владельца -> в канал сети."""
    try:
        if not _enabled():
            return
        payload = {
            "type": "msg",
            "channel_id": channel_id,
            "msg": {k: msg.get(k) for k in ("id", "channel_id", "sender_id", "content",
                                            "sticker", "attachment", "reply_to", "edited_at",
                                            "is_pinned", "created_at") if k in msg},
            "from_node": _node_id(),
            "from_profile": profile_id,
            "from_name": _local_display_name(profile_id),
            "msg_id": msg.get("id") or "",
        }
        _publish_server_event(server_id, payload)
    except Exception as e:
        logger.error(f"social-net: hook server msg: {e}")


def init_net_db():
    """Создать таблицы (вызывается из social_module._init_db)."""
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS social_net_servers (
        id TEXT PRIMARY KEY,
        owner_node TEXT NOT NULL,
        name TEXT DEFAULT '',
        channels TEXT DEFAULT '[]',
        members TEXT DEFAULT '{}',
        messages TEXT DEFAULT '{}',
        updated_at REAL DEFAULT 0
    );
    """)
    conn.commit()
