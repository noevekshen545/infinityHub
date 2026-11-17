"""Модуль социальной сети — профили, стена, друзья, сообщения, лента, уведомления."""

import os
import json
import time
import uuid
import sqlite3
import threading
import shutil
import mimetypes
import re
import httpx
from datetime import datetime
from urllib.parse import urlparse, quote

from fastapi import APIRouter, Query, Path, Body, Request, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List
import swallow

logger = None
try:
    import server
    logger = server.logger
except ImportError:
    import logging
    logger = logging.getLogger("social")

_DATA_ROOT = os.path.expanduser(os.path.join("~", ".infinity", "data"))
_DATA_ROOT = os.environ.get("INFINITY_DATA_DIR") or _DATA_ROOT
DATA_DIR = _DATA_ROOT
SOCIAL_DIR = os.path.join(DATA_DIR, "social")
UPLOADS_DIR = os.path.join(SOCIAL_DIR, "uploads")
AVATARS_DIR = os.path.join(UPLOADS_DIR, "avatars")
COVERS_DIR = os.path.join(UPLOADS_DIR, "covers")
POST_MEDIA_DIR = os.path.join(UPLOADS_DIR, "post_media")
STORIES_DIR = os.path.join(UPLOADS_DIR, "stories")
VOICE_DIR = os.path.join(UPLOADS_DIR, "voice")
CHAT_MEDIA_DIR = os.path.join(UPLOADS_DIR, "chat_media")
STICKERS_DIR = os.path.join(UPLOADS_DIR, "stickers")
DB_PATH = os.path.join(DATA_DIR, "social.db")

_OLD_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
def _migrate_old_data():
    old_db = os.path.join(_OLD_DATA_DIR, "social.db")
    old_social = os.path.join(_OLD_DATA_DIR, "social")
    if not os.path.exists(old_db) and not os.path.exists(old_social):
        return
    for d in [SOCIAL_DIR, UPLOADS_DIR, AVATARS_DIR, COVERS_DIR, POST_MEDIA_DIR, STORIES_DIR, VOICE_DIR, CHAT_MEDIA_DIR, STICKERS_DIR]:
        os.makedirs(d, exist_ok=True)
    if os.path.exists(old_db) and (not os.path.exists(DB_PATH) or os.path.getmtime(old_db) > os.path.getmtime(DB_PATH)):
        shutil.copy2(old_db, DB_PATH)
        logger.info(f"Migrated social.db from {old_db} to {DB_PATH}")
    if os.path.exists(old_social):
        for root, dirs, files in os.walk(old_social):
            rel = os.path.relpath(root, old_social)
            dest = os.path.join(SOCIAL_DIR, rel)
            os.makedirs(dest, exist_ok=True)
            for f in files:
                src_f = os.path.join(root, f)
                dst_f = os.path.join(dest, f)
                if not os.path.exists(dst_f):
                    shutil.copy2(src_f, dst_f)
                    logger.info(f"Migrated {src_f} -> {dst_f}")
if not os.environ.get("INFINITY_DATA_DIR"):
    _migrate_old_data()

for d in [SOCIAL_DIR, UPLOADS_DIR, AVATARS_DIR, COVERS_DIR, POST_MEDIA_DIR, STORIES_DIR, VOICE_DIR, CHAT_MEDIA_DIR, STICKERS_DIR]:
    os.makedirs(d, exist_ok=True)

_ALLOWED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg'}
_ALLOWED_VIDEO_EXT = {'.mp4', '.webm', '.mov', '.mkv', '.avi'}
_ALLOWED_AUDIO_EXT = {'.mp3', '.ogg', '.wav', '.m4a', '.flac', '.opus', '.aac'}
_ALLOWED_FILE_EXT = _ALLOWED_IMAGE_EXT | _ALLOWED_VIDEO_EXT | _ALLOWED_AUDIO_EXT | {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.zip', '.rar', '.7z'}

def _guess_media_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _ALLOWED_IMAGE_EXT:
        return "image"
    if ext in _ALLOWED_VIDEO_EXT:
        return "video"
    if ext in _ALLOWED_AUDIO_EXT:
        return "audio"
    return "file"

# --- Upload validation: magic bytes, content sniffing, size caps ---

_MAX_IMAGE_BYTES = 15 * 1024 * 1024
_MAX_CHAT_FILE_BYTES = 200 * 1024 * 1024
_MAX_POST_MEDIA_BYTES = 100 * 1024 * 1024

_IMAGE_MAGIC = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF8",),
    ".webp": (b"RIFF",),
}

_DANGEROUS_MARKERS = (b"<script", b"<html", b"<!doctype", b"javascript:", b"onerror=", b"onload=")

def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, "Файл слишком большой")
    return data

def _validate_image_bytes(ext: str, data: bytes):
    magic_list = _IMAGE_MAGIC.get(ext)
    if magic_list and not any(data.startswith(m) for m in magic_list):
        raise HTTPException(400, "Содержимое файла не соответствует расширению")
    if ext == ".webp" and (len(data) < 12 or data[8:12] != b"WEBP"):
        raise HTTPException(400, "Недопустимый WebP файл")
    head = data[:2048].lower()
    if any(m in head for m in _DANGEROUS_MARKERS):
        raise HTTPException(400, "Недопустимое содержимое файла")

def _validate_generic_bytes(ext: str, data: bytes):
    if ext in _ALLOWED_IMAGE_EXT:
        _validate_image_bytes(ext, data)
        return
    head = data[:512].lower()
    if b"<script" in head or b"<html" in head or b"<!doctype" in head:
        raise HTTPException(400, "Недопустимое содержимое файла")

def _save_upload(file: UploadFile, filepath: str, max_bytes: int, image_only: bool = False):
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    data = _read_upload(file, max_bytes)
    if image_only:
        _validate_image_bytes(ext, data)
    else:
        _validate_generic_bytes(ext, data)
    with open(filepath, "wb") as f:
        f.write(data)
    return ext, os.path.getsize(filepath)

# --- Simple in-memory rate limiter ---
_rate_limit_store = {}
_rate_limit_lock = threading.Lock()
_rate_limit_last_sweep = 0.0

def _check_rate_limit(key: str, max_requests: int, window: float = 60.0):
    global _rate_limit_last_sweep
    now = time.time()
    with _rate_limit_lock:
        # Периодически выбрасываем ключи, у которых окно давно прошло.
        # Без этого _rate_limit_store рос бесконечно: ключи вида
        # "msg:<profile_id>" жили до перезапуска процесса.
        if now - _rate_limit_last_sweep > window:
            _rate_limit_last_sweep = now
            stale = [k for k, b in _rate_limit_store.items()
                     if not b or b[-1] <= now - window]
            for k in stale:
                _rate_limit_store.pop(k, None)
        bucket = _rate_limit_store.get(key)
        if bucket is None:
            _rate_limit_store[key] = [now]
            return True
        cutoff = now - window
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= max_requests:
            return False
        bucket.append(now)
        return True

# Роли, которым разрешено модерировать группу. Владелец намеренно
# включён: часть проверок (форум, альбомы) его уже учитывала, а стена
# группы — нет, из-за чего создатель группы не мог закрепить чужой пост.
_GROUP_MOD_ROLES = ("owner", "admin", "moderator")

router = APIRouter(prefix="/api/social", tags=["social"])

# --- Pydantic models ---

class PostCreate(BaseModel):
    content: Optional[str] = ""
    privacy: int = 0
    attachments: str = "[]"

class CommentCreate(BaseModel):
    content: str
    parent_id: Optional[str] = None

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    status: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    birthday: Optional[str] = None
    privacy_level: Optional[int] = None
    profile_privacy: Optional[int] = None

class MessageCreate(BaseModel):
    content: str
    attachments: str = "[]"
    reply_to_id: Optional[str] = None
    ttl: float = 0

class ConversationCreate(BaseModel):
    title: str = ""
    member_ids: List[str]

class MessageEditBody(BaseModel):
    content: str

class ChatFolderCreate(BaseModel):
    name: str

class ChatFolderMove(BaseModel):
    folder_id: str = ""

# --- Database ---

_db_local = threading.local()

def _get_db() -> sqlite3.Connection:
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
        except sqlite3.OperationalError:
            swallow.note('social_module._get_db:L227')
            pass
        # synchronous=NORMAL при WAL означает, что SQLite НЕ сбрасывает
        # транзакцию на диск при каждом commit — она живёт только в
        # кэше ОС. Если процесс убить (recovery_guard, Ctrl+C, краш) или
        # вырубить питание, свежие посты/сообщения/лайки терялись, и
        # это было ровно то «в соцсети данные тоже пропадают».
        # FULL синхронизирует каждую транзакцию — медленнее, но данные
        # гарантированно на диске.
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _db_local.conn = conn
    return conn

def _init_db():
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS social_users (
        profile_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL DEFAULT '',
        bio TEXT DEFAULT '',
        avatar_path TEXT DEFAULT '',
        cover_path TEXT DEFAULT '',
        status TEXT DEFAULT '',
        location TEXT DEFAULT '',
        website TEXT DEFAULT '',
        birthday TEXT DEFAULT '',
        joined_at REAL NOT NULL,
        last_seen REAL DEFAULT 0,
        is_online INTEGER DEFAULT 0,
        privacy_level INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS social_posts (
        id TEXT PRIMARY KEY,
        author_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        is_pinned INTEGER DEFAULT 0,
        privacy INTEGER DEFAULT 0,
        attachments TEXT DEFAULT '[]',
        likes_count INTEGER DEFAULT 0,
        comments_count INTEGER DEFAULT 0,
        repost_id TEXT DEFAULT NULL,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_comments (
        id TEXT PRIMARY KEY,
        post_id TEXT,
        author_id TEXT NOT NULL,
        parent_id TEXT DEFAULT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        likes_count INTEGER DEFAULT 0,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_likes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(user_id, target_type, target_id)
    );

    CREATE TABLE IF NOT EXISTS social_friendships (
        id TEXT PRIMARY KEY,
        requester_id TEXT NOT NULL,
        addressee_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        UNIQUE(requester_id, addressee_id),
        FOREIGN KEY (requester_id) REFERENCES social_users(profile_id),
        FOREIGN KEY (addressee_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_conversations (
        id TEXT PRIMARY KEY,
        title TEXT DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        is_group INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS social_conversation_members (
        conversation_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        joined_at REAL NOT NULL,
        last_read_at REAL DEFAULT 0,
        PRIMARY KEY (conversation_id, profile_id),
        FOREIGN KEY (conversation_id) REFERENCES social_conversations(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL,
        edited_at REAL DEFAULT 0,
        is_read INTEGER DEFAULT 0,
        attachments TEXT DEFAULT '[]',
        reply_to_id TEXT DEFAULT NULL,
        FOREIGN KEY (conversation_id) REFERENCES social_conversations(id) ON DELETE CASCADE,
        FOREIGN KEY (sender_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_chat_folders (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (owner_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_pinned_messages (
        message_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        pinned_at REAL NOT NULL,
        PRIMARY KEY (message_id, profile_id),
        FOREIGN KEY (message_id) REFERENCES social_messages(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_notifications (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        target_type TEXT DEFAULT '',
        target_id TEXT DEFAULT '',
        content TEXT DEFAULT '',
        is_read INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        FOREIGN KEY (user_id) REFERENCES social_users(profile_id),
        FOREIGN KEY (actor_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_feeds (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        post_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (user_id) REFERENCES social_users(profile_id),
        FOREIGN KEY (post_id) REFERENCES social_posts(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_posts_author ON social_posts(author_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_comments_post ON social_comments(post_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_likes_target ON social_likes(target_type, target_id);
    CREATE INDEX IF NOT EXISTS idx_friendships_status ON social_friendships(status);
    CREATE INDEX IF NOT EXISTS idx_friendships_user ON social_friendships(requester_id, addressee_id);
    CREATE INDEX IF NOT EXISTS idx_messages_conv ON social_messages(conversation_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_notifications_user ON social_notifications(user_id, created_at DESC);
    
    CREATE TABLE IF NOT EXISTS social_reactions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        post_id TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'like',
        created_at REAL NOT NULL,
        UNIQUE(user_id, post_id),
        FOREIGN KEY (post_id) REFERENCES social_posts(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_stories (
        id TEXT PRIMARY KEY,
        author_id TEXT NOT NULL,
        media_url TEXT NOT NULL,
        media_type TEXT DEFAULT 'image',
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_viewed_stories (
        story_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        PRIMARY KEY (story_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS social_groups (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        avatar_path TEXT DEFAULT '',
        cover_path TEXT DEFAULT '',
        owner_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        member_count INTEGER DEFAULT 1,
        FOREIGN KEY (owner_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_group_members (
        group_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        role TEXT DEFAULT 'member',
        joined_at REAL NOT NULL,
        PRIMARY KEY (group_id, profile_id),
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_group_posts (
        id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL,
        is_pinned INTEGER DEFAULT 0,
        poll_id TEXT DEFAULT '',
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_group_polls (
        id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL DEFAULT 0,
        is_closed INTEGER DEFAULT 0,
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_group_poll_votes (
        poll_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        option_index INTEGER NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (poll_id, profile_id),
        FOREIGN KEY (poll_id) REFERENCES social_group_polls(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_group_join_requests (
        id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        message TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        created_at REAL NOT NULL,
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_group_bans (
        group_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        banned_by TEXT NOT NULL,
        reason TEXT DEFAULT '',
        created_at REAL NOT NULL,
        PRIMARY KEY (group_id, profile_id),
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_group_chat_messages (
        id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL,
        attachments TEXT DEFAULT '[]',
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE,
        FOREIGN KEY (sender_id) REFERENCES social_users(profile_id)
    );

    -- Форум группы. Структура: раздел -> тема -> посты (ответы).
    -- Первый пост темы (is_first=1) хранится вместе с остальными: так он
    -- единообразно лайкается, редактируется и подчиняется тем же правам.
    CREATE TABLE IF NOT EXISTS social_forum_sections (
        id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        position INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_forum_threads (
        id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        section_id TEXT DEFAULT '',
        author_id TEXT NOT NULL,
        title TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        is_pinned INTEGER DEFAULT 0,
        is_closed INTEGER DEFAULT 0,
        views_count INTEGER DEFAULT 0,
        posts_count INTEGER DEFAULT 0,
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_forum_posts (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        author_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at REAL NOT NULL,
        edited_at REAL DEFAULT 0,
        is_first INTEGER DEFAULT 0,
        likes_count INTEGER DEFAULT 0,
        FOREIGN KEY (thread_id) REFERENCES social_forum_threads(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_forum_post_likes (
        post_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (post_id, profile_id),
        FOREIGN KEY (post_id) REFERENCES social_forum_posts(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS social_photo_comment_likes (
        comment_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (comment_id, profile_id),
        FOREIGN KEY (comment_id) REFERENCES social_photo_comments(id) ON DELETE CASCADE
    );

    -- Версии правок фото. Оригинал не перезаписывается: каждая правка
    -- кладётся отдельным файлом, здесь хранится история и текущая версия.
    CREATE TABLE IF NOT EXISTS social_photo_versions (
        id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL,
        path TEXT NOT NULL,
        ops TEXT NOT NULL,
        author_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (photo_id) REFERENCES social_profile_photos(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_photo_versions_photo
        ON social_photo_versions(photo_id, created_at DESC);

    CREATE TABLE IF NOT EXISTS social_p2p_signals (
        id TEXT PRIMARY KEY,
        from_id TEXT NOT NULL,
        to_id TEXT NOT NULL,
        conversation_id TEXT,
        signal TEXT NOT NULL,
        created_at REAL NOT NULL,
        is_consumed INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_feeds_user ON social_feeds(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_stories_expires ON social_stories(expires_at);
    CREATE INDEX IF NOT EXISTS idx_p2p_signals ON social_p2p_signals(to_id, is_consumed);

    -- Profile extras table
    CREATE TABLE IF NOT EXISTS social_profile_extras (
        profile_id TEXT NOT NULL,
        field_key TEXT NOT NULL,
        field_value TEXT DEFAULT '',
        privacy INTEGER DEFAULT 0,
        PRIMARY KEY (profile_id, field_key),
        FOREIGN KEY (profile_id) REFERENCES social_users(profile_id)
    );

    -- Profile photos gallery
    CREATE TABLE IF NOT EXISTS social_profile_photos (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        url TEXT NOT NULL,
        caption TEXT DEFAULT '',
        created_at REAL NOT NULL,
        FOREIGN KEY (profile_id) REFERENCES social_users(profile_id)
    );

    -- Photo albums
    CREATE TABLE IF NOT EXISTS social_albums (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        cover_path TEXT DEFAULT '',
        created_at REAL NOT NULL,
        photo_count INTEGER DEFAULT 0,
        privacy INTEGER DEFAULT 0,
        -- Альбом принадлежит профилю (обе пусты), группе (заполнен group_id)
        -- или каналу сервера (заполнен channel_id). Подробнее — social_scope.py.
        group_id TEXT DEFAULT '',
        channel_id TEXT DEFAULT '',
        FOREIGN KEY (profile_id) REFERENCES social_users(profile_id)
    );

    -- Album-photo mapping
    CREATE TABLE IF NOT EXISTS social_album_photos (
        album_id TEXT NOT NULL,
        photo_id TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0,
        added_at REAL NOT NULL,
        PRIMARY KEY (album_id, photo_id),
        FOREIGN KEY (album_id) REFERENCES social_albums(id) ON DELETE CASCADE,
        FOREIGN KEY (photo_id) REFERENCES social_profile_photos(id) ON DELETE CASCADE
    );

    -- Photo likes
    CREATE TABLE IF NOT EXISTS social_photo_likes (
        photo_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (photo_id, profile_id),
        FOREIGN KEY (photo_id) REFERENCES social_profile_photos(id) ON DELETE CASCADE
    );

    -- Photo person tags (area selection)
    CREATE TABLE IF NOT EXISTS social_photo_tags (
        id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        name TEXT DEFAULT '',
        x REAL DEFAULT 0,
        y REAL DEFAULT 0,
        w REAL DEFAULT 0,
        h REAL DEFAULT 0,
        created_at REAL NOT NULL,
        FOREIGN KEY (photo_id) REFERENCES social_profile_photos(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_photo_tags_photo ON social_photo_tags(photo_id);

    -- Photo comments
    CREATE TABLE IF NOT EXISTS social_photo_comments (
        id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (photo_id) REFERENCES social_profile_photos(id) ON DELETE CASCADE
    );

    -- ActivityPub remote cache
    CREATE TABLE IF NOT EXISTS social_ap_cache (
        actor_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        domain TEXT NOT NULL,
        inbox_url TEXT NOT NULL,
        outbox_url TEXT DEFAULT '',
        public_key_pem TEXT DEFAULT '',
        avatar_url TEXT DEFAULT '',
        display_name TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        followers_url TEXT DEFAULT '',
        following_url TEXT DEFAULT '',
        cached_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS social_hashtags (
        id TEXT PRIMARY KEY,
        tag TEXT NOT NULL COLLATE NOCASE,
        post_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (post_id) REFERENCES social_posts(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_hashtags_tag ON social_hashtags(tag, created_at DESC);

    CREATE TABLE IF NOT EXISTS social_mentions (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL COLLATE NOCASE,
        post_id TEXT,
        comment_id TEXT,
        mentioned_id TEXT,
        author_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        is_read INTEGER DEFAULT 0,
        FOREIGN KEY (post_id) REFERENCES social_posts(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_mentions_user ON social_mentions(mentioned_id, is_read, created_at DESC);

    -- Multiple avatars per user (original + crop params)
    CREATE TABLE IF NOT EXISTS social_avatars (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        path TEXT NOT NULL,
        crop TEXT DEFAULT '{"zoom":1,"x":0.5,"y":0.5}',
        is_active INTEGER DEFAULT 0,
        sort_order INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        FOREIGN KEY (profile_id) REFERENCES social_users(profile_id)
    );
    CREATE INDEX IF NOT EXISTS idx_avatars_user ON social_avatars(profile_id, sort_order);

    -- Sticker packs
    CREATE TABLE IF NOT EXISTS social_sticker_packs (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS social_stickers (
        id TEXT PRIMARY KEY,
        pack_id TEXT NOT NULL,
        url TEXT NOT NULL,
        emoji TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        FOREIGN KEY (pack_id) REFERENCES social_sticker_packs(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_stickers_pack ON social_stickers(pack_id, sort_order);

    -- Remote (federated) follows
    CREATE TABLE IF NOT EXISTS social_remote_follows (
        profile_id TEXT NOT NULL,
        actor_url TEXT NOT NULL,
        username TEXT NOT NULL,
        domain TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        avatar_url TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        created_at REAL NOT NULL,
        PRIMARY KEY (profile_id, actor_url),
        FOREIGN KEY (profile_id) REFERENCES social_users(profile_id)
    );

    -- Known ActivityPub instances (LAN discovery)
    CREATE TABLE IF NOT EXISTS social_ap_instances (
        domain TEXT PRIMARY KEY,
        protocol TEXT DEFAULT 'http',
        name TEXT DEFAULT '',
        verified_at REAL DEFAULT 0,
        created_at REAL NOT NULL
    );

    -- Pairing codes for instance-to-instance connection
    CREATE TABLE IF NOT EXISTS social_ap_pairing (
        code TEXT PRIMARY KEY,
        local_domain TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        remote_domain TEXT DEFAULT '',
        remote_name TEXT DEFAULT ''
    );

    -- Gossip peer exchange state
    CREATE TABLE IF NOT EXISTS social_ap_peers (
        domain TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        last_seen REAL DEFAULT 0,
        created_at REAL NOT NULL
    );

    -- Discord-like servers (rooms)
    CREATE TABLE IF NOT EXISTS social_servers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        icon_path TEXT DEFAULT '',
        owner_id TEXT NOT NULL,
        invite_code TEXT NOT NULL,
        is_private INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS social_server_members (
        server_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        role TEXT DEFAULT 'member',
        joined_at REAL NOT NULL,
        PRIMARY KEY (server_id, profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_server_channels (
        id TEXT PRIMARY KEY,
        server_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT DEFAULT 'text',
        is_private INTEGER DEFAULT 0,
        access_json TEXT DEFAULT '[]',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS social_server_messages (
        id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        content TEXT DEFAULT '',
        sticker TEXT DEFAULT '',
        attachment TEXT DEFAULT '',
        reply_to TEXT DEFAULT '',
        edited_at REAL DEFAULT 0,
        is_pinned INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS social_server_reactions (
        message_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        emoji TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (message_id, profile_id, emoji)
    );

    CREATE TABLE IF NOT EXISTS social_group_chat_reactions (
        message_id TEXT NOT NULL,
        group_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        emoji TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (message_id, profile_id, emoji)
    );

    CREATE INDEX IF NOT EXISTS idx_group_chat_react ON social_group_chat_reactions(message_id);

    CREATE TABLE IF NOT EXISTS social_group_invites (
        code TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        created_by TEXT NOT NULL,
        role TEXT DEFAULT 'member',
        uses INTEGER DEFAULT 0,
        max_uses INTEGER DEFAULT 0,
        expires_at REAL DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_group_invites ON social_group_invites(group_id);

    CREATE TABLE IF NOT EXISTS social_channel_views (
        channel_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        viewed_at REAL NOT NULL,
        PRIMARY KEY (channel_id, profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_post_views (
        post_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        viewed_at REAL NOT NULL,
        PRIMARY KEY (post_id, profile_id)
    );

    CREATE INDEX IF NOT EXISTS idx_server_msg ON social_server_messages(channel_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_server_members ON social_server_members(server_id);
    CREATE INDEX IF NOT EXISTS idx_server_channels ON social_server_channels(server_id);

    -- Индексы групп. Без них лента группы, список альбомов и форум
    -- сканировали таблицу целиком: колонки group_id нигде не были
    -- проиндексированы, а выборки идут именно по ним.
    CREATE INDEX IF NOT EXISTS idx_group_posts_feed
        ON social_group_posts(group_id, is_pinned DESC, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_group_members_user
        ON social_group_members(profile_id);
    -- idx_albums_group создан ниже, после миграций: колонки group_id/kind
    -- появляются только там, а ошибка внутри этого скрипта обрывала всю
    -- инициализацию — на чистой базе не досоздавались 9 таблиц и 15 индексов.

    -- Форум: разделы по порядку, темы по свежести активности внутри
    -- раздела и по всей группе, посты темы по хронологии.
    CREATE INDEX IF NOT EXISTS idx_forum_sections_group
        ON social_forum_sections(group_id, position, created_at);
    CREATE INDEX IF NOT EXISTS idx_forum_threads_group
        ON social_forum_threads(group_id, is_pinned DESC, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_forum_threads_section
        ON social_forum_threads(section_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_forum_posts_thread
        ON social_forum_posts(thread_id, created_at);

    -- ============ Боты в мессенджере ============
    -- id совпадает с profile_id записи в social_users (с префиксом bot:),
    -- чтобы отправителя сообщений не пришлось заводить отдельно.
    CREATE TABLE IF NOT EXISTS social_bots (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT DEFAULT '',
        avatar_path TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        system_prompt TEXT DEFAULT '',
        model TEXT DEFAULT '',
        temperature REAL DEFAULT 0.7,
        max_context INTEGER DEFAULT 20,
        is_enabled INTEGER DEFAULT 1,
        is_public INTEGER DEFAULT 0,
        triggers TEXT DEFAULT '["mention"]',
        keywords TEXT DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        FOREIGN KEY (owner_id) REFERENCES social_users(profile_id)
    );

    CREATE TABLE IF NOT EXISTS social_bot_chats (
        bot_id TEXT NOT NULL,
        chat_kind TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        added_at REAL NOT NULL,
        PRIMARY KEY (bot_id, chat_kind, chat_id)
    );

    CREATE INDEX IF NOT EXISTS idx_bot_chats ON social_bot_chats(chat_kind, chat_id);
    CREATE INDEX IF NOT EXISTS idx_bots_owner ON social_bots(owner_id);
    CREATE TABLE IF NOT EXISTS social_bot_log (
        id TEXT PRIMARY KEY,
        bot_id TEXT NOT NULL,
        chat_kind TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_bot_log ON social_bot_log(bot_id, chat_kind, chat_id, created_at);

    -- ============ Очередь обновлений для внешних ботов ============
    -- Строка появляется, когда в чат с ботом кто-то написал, и живёт до
    -- успешной выдачи через getUpdates. Тот же приём, что в телеграме:
    -- бот может быть офлайн, накопленное не теряется.
    -- update_id сквозной и растёт: getUpdates с offset=next_update_id+1
    -- отдаёт строго новые, уже доставленные не повторяются.
    CREATE TABLE IF NOT EXISTS social_bot_updates (
        update_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        chat_kind TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT DEFAULT 'pending',     -- pending | delivered | failed
        attempts INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        delivered_at REAL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_bot_updates ON social_bot_updates(bot_id, status, update_id);
    CREATE INDEX IF NOT EXISTS idx_bot_updates_ttl ON social_bot_updates(status, created_at);

    -- ============ Аудит вызовов Bot API ============
    -- Кто из ботов что вызывал и сколько это заняло. Нужен и для отладки
    -- ("бот молчит" — видно, что он вообще не приходит), и для безопасности.
    CREATE TABLE IF NOT EXISTS social_bot_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        method TEXT NOT NULL,
        ok INTEGER DEFAULT 1,
        ms INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_bot_calls ON social_bot_calls(bot_id, created_at DESC);

    -- Настройки ботов. Пока одна: allow_private_webhooks разрешает боту
    -- принимать вебхуки на приватные адреса (нужно для локальной установки,
    -- где сервер и бот на одной машине). По умолчанию выключено.
    CREATE TABLE IF NOT EXISTS social_bot_settings (
        key TEXT PRIMARY KEY,
        val TEXT DEFAULT ''
    );

    -- ============ Статьи групп и каналов ============
    -- Статья состоит из заголовка, обложки и произвольного числа секций.
    -- Разметка внутри секции хранится как Markdown (план: «и то и то» —
    -- для Markdown есть предпросмотр без редактора, WYSIWYG переводится
    -- в Markdown перед сохранением, поэтому в базе один формат).
    CREATE TABLE IF NOT EXISTS social_articles (
        id TEXT PRIMARY KEY,
        -- Статья принадлежит либо группе, либо каналу сервера. Заполнена
        -- ровно одна из двух колонок, поэтому обе допускают NULL.
        group_id TEXT,
        channel_id TEXT DEFAULT '',
        author_id TEXT NOT NULL,
        title TEXT NOT NULL,
        excerpt TEXT DEFAULT '',
        cover_image TEXT DEFAULT '',
        status TEXT DEFAULT 'draft',   -- draft | published | review | rejected
        views_count INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE,
        FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
    );
    CREATE INDEX IF NOT EXISTS idx_articles_group
        ON social_articles(group_id, status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_articles_channel
        ON social_articles(channel_id, status, updated_at DESC);

    CREATE TABLE IF NOT EXISTS social_article_sections (
        id TEXT PRIMARY KEY,
        article_id TEXT NOT NULL,
        title TEXT DEFAULT '',
        content TEXT DEFAULT '',
        position INTEGER DEFAULT 0,
        FOREIGN KEY (article_id) REFERENCES social_articles(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_article_sections
        ON social_article_sections(article_id, position);

    """)
    conn.commit()
    try:
        import social_net_module
        social_net_module.init_net_db()
    except Exception:
        swallow.note('social_module._init_db:L1024')
        pass
    # Safe migrations: add columns if not exist
    group_migrations = [
        ("social_groups", "privacy", "TEXT DEFAULT 'public'"),
        ("social_groups", "is_channel", "INTEGER DEFAULT 0"),
        ("social_groups", "category", "TEXT DEFAULT ''"),
        ("social_groups", "tags", "TEXT DEFAULT ''"),
        ("social_groups", "last_activity", "REAL DEFAULT 0"),
        ("social_groups", "link", "TEXT DEFAULT ''"),
        ("social_group_posts", "attachments", "TEXT DEFAULT '[]'"),
        ("social_group_posts", "likes_count", "INTEGER DEFAULT 0"),
        ("social_comments", "target_type", "TEXT DEFAULT 'post'"),
        ("social_comments", "target_id", "TEXT DEFAULT ''"),
        ("social_users", "profile_extra", "TEXT DEFAULT '{}'"),
        ("social_users", "privacy_settings", "TEXT DEFAULT '{}'"),
        ("social_users", "is_federated", "INTEGER DEFAULT 0"),
        ("social_users", "federation_url", "TEXT DEFAULT ''"),
        ("social_users", "profile_privacy", "INTEGER DEFAULT 0"),
        ("social_groups", "post_permission", "INTEGER DEFAULT 0"),
        ("social_group_posts", "is_pinned", "INTEGER DEFAULT 0"),
        ("social_group_posts", "poll_id", "TEXT DEFAULT ''"),
        ("social_group_posts", "views_count", "INTEGER DEFAULT 0"),
        ("social_group_posts", "edited_at", "REAL DEFAULT 0"),
        ("social_group_chat_messages", "attachments", "TEXT DEFAULT '[]'"),
        ("social_group_chat_messages", "reply_to", "TEXT DEFAULT ''"),
        ("social_group_chat_messages", "is_pinned", "INTEGER DEFAULT 0"),
        ("social_group_chat_messages", "edited_at", "REAL DEFAULT 0"),
        ("social_group_chat_messages", "system", "TEXT DEFAULT ''"),
        ("social_albums", "privacy", "INTEGER DEFAULT 0"),
        # Привязка к каналу сервера. Раньше альбомы и статьи знали только
        # о группе; channel_id пуст для профиля и группы, заполнен для
        # канала сервера. Подробнее — в social_scope.py.
        ("social_albums", "channel_id", "TEXT DEFAULT ''"),
        ("social_articles", "channel_id", "TEXT DEFAULT ''"),
        ("social_users", "avatar_crop", "TEXT DEFAULT '{\"zoom\":1,\"x\":0.5,\"y\":0.5}'"),
        ("social_users", "ap_actor_url", "TEXT DEFAULT ''"),
        ("social_users", "ap_remote_username", "TEXT DEFAULT ''"),
        ("social_users", "ap_remote_domain", "TEXT DEFAULT ''"),
        ("social_posts", "federation_id", "TEXT DEFAULT ''"),
        ("social_messages", "system", "TEXT DEFAULT ''"),
        ("social_messages", "ttl", "REAL DEFAULT 0"),
        ("social_conversation_members", "is_pinned", "INTEGER DEFAULT 0"),
        ("social_conversation_members", "is_muted", "INTEGER DEFAULT 0"),
        ("social_conversation_members", "folder_id", "TEXT DEFAULT ''"),
        ("social_conversation_members", "is_archived", "INTEGER DEFAULT 0"),
        ("social_albums", "visibility", "TEXT DEFAULT 'public'"),
        ("social_server_messages", "reply_to", "TEXT DEFAULT ''"),
        ("social_server_messages", "edited_at", "REAL DEFAULT 0"),
        ("social_server_messages", "is_pinned", "INTEGER DEFAULT 0"),
        # Бот — это обычная запись social_users с флагом is_bot: все
        # сообщения ссылаются на отправителя через FK на social_users,
        # поэтому отдельная сущность без записи здесь сломала бы JOIN-ы.
        ("social_users", "is_bot", "INTEGER DEFAULT 0"),
        # Внешние боты (Telegram-совместимый Bot API). kind='llm' — нынешний
        # поведение (внутренний вызов /api/ask), kind='external' — бот-сервис
        # на своём хостинге, ходит по токену. token_hash хранит только sha256
        # секрета: сам токен показывается владельцу один раз при выдаче.
        ("social_bots", "kind", "TEXT DEFAULT 'llm'"),
        ("social_bots", "token_hash", "TEXT DEFAULT ''"),
        ("social_bots", "token_prefix", "TEXT DEFAULT ''"),
        ("social_bots", "bot_url", "TEXT DEFAULT ''"),
        ("social_bots", "transport", "TEXT DEFAULT 'long_poll'"),
        ("social_bots", "secret", "TEXT DEFAULT ''"),
        ("social_bots", "commands", "TEXT DEFAULT '[]'"),
        ("social_bots", "last_error", "TEXT DEFAULT ''"),
        ("social_bots", "last_seen_at", "REAL DEFAULT 0"),
        ("social_bots", "next_update_id", "INTEGER DEFAULT 0"),
        # Альбомы групп и каналов. Не заводим отдельные таблицы для видео и
        # аудио: это те же альбомы, отличается лишь тип содержимого.
        # group_id пустой = альбом профиля (прежнее поведение), privacy
        # расширен значениями 2 (приватный) и 3 (по ссылке).
        ("social_albums", "group_id", "TEXT DEFAULT ''"),
        ("social_albums", "kind", "TEXT DEFAULT 'photo'"),
        ("social_albums", "updated_at", "REAL DEFAULT 0"),
        ("social_album_photos", "media_type", "TEXT DEFAULT 'photo'"),
        # Откуда взялось фото: 'profile' — загружено в профиль, 'album' —
        # в альбом группы или канала. Раньше колонки не было, и фото из
        # альбома попадало в профильную галерею, хотя автор этого не
        # просил. Старые строки считаются профильными.
        ("social_profile_photos", "origin", "TEXT DEFAULT 'profile'"),
        # Комментарии к фото приведены к общему виду. У social_comments
        # есть content/parent_id/likes_count/updated_at, у social_photo_comments
        # был только text без ответов и лайков. Добавляем недостающее;
        # старая колонка text остаётся как есть, чтобы ничего не потерять —
        # чтение идёт через COALESCE(content, text).
        ("social_photo_comments", "content", "TEXT DEFAULT ''"),
        ("social_photo_comments", "parent_id", "TEXT DEFAULT ''"),
        ("social_photo_comments", "likes_count", "INTEGER DEFAULT 0"),
        # Мягкий триаж текста движком laya (спам/тон/срочность). Хранится
        # вердикт-подсказка, посты из-за этого не удаляются: решение всегда
        # за пользователем. Пусто = движок не сработал.
        ("social_posts", "laya_triage", "TEXT DEFAULT ''"),
        ("social_comments", "laya_triage", "TEXT DEFAULT ''"),
        ("social_group_posts", "laya_triage", "TEXT DEFAULT ''"),
        ("social_photo_comments", "edited_at", "REAL DEFAULT 0"),
        # Счётчик ответов в теме форума. Держится в базе, а не считается
        # COUNT()-ом на каждом рендере списка тем.
        ("social_forum_threads", "posts_count", "INTEGER DEFAULT 0"),
        # Неразрушающее редактирование фото: версии лежат отдельными
        # файлами, в social_profile_photos хранится текущая и время правки.
        ("social_profile_photos", "current_version", "TEXT DEFAULT ''"),
        ("social_profile_photos", "edited_at", "REAL DEFAULT 0"),
        # Репост в стену группы. Раньше repost_id был только у social_posts,
        # поэтому поделиться постом группы или фото альбома было нечем.
        ("social_group_posts", "repost_id", "TEXT DEFAULT NULL"),
        ("social_group_posts", "repost_group_id", "TEXT DEFAULT ''"),
        # Вложения в сообщениях форума: картинки и файлы в ответах.
        ("social_forum_posts", "attachments", "TEXT DEFAULT '[]'"),
    ]
    _known_tables = {"social_users", "social_posts", "social_messages", "social_groups", "social_group_posts", "social_comments", "social_group_chat_messages", "social_albums", "social_conversation_members", "social_conversations", "social_profile_photos", "social_server_messages", "social_server_reactions", "social_bots", "social_bot_chats", "social_bot_log",
           "social_albums", "social_album_photos",
           # Статей не было в списке, и миграция channel_id для них
           # пропускалась с предупреждением — колонка не появлялась.
           "social_articles", "social_article_sections",
           # Комментарии к фото и форум. Без этих таблиц в списке миграции
           # для них пропускались с предупреждением, и колонки не появлялись.
           "social_photo_comments", "social_forum_sections",
           "social_forum_threads", "social_forum_posts",
           "social_forum_post_likes", "social_photo_versions" }
    for table, col, col_type in group_migrations:
        if table not in _known_tables:
            logger.warning(f"Migration skipped: unknown table '{table}'")
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except Exception as e:
            logger.debug(f"Migration skipped ({table}.{col}): {e}")
    conn.commit()
    _relax_articles_group_id(conn)
    _relax_comments_post_id(conn)
    # Лента альбомов группы ищется по (group_id, kind); обе колонки добавлены
    # миграциями выше, поэтому индекс создаётся здесь, а не в общем скрипте.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_albums_group ON social_albums(group_id, kind)")
    conn.commit()


def _relax_articles_group_id(conn):
    """Убрать NOT NULL с social_articles.group_id.

    Статья принадлежит либо группе, либо каналу сервера, поэтому у группы
    быть не должно. ALTER TABLE в SQLite не умеет менять ограничения —
    таблицу приходится пересоздавать. Колонки и данные копируются как есть.

    Вызывается после основных миграций и на каждом старте, но почти всегда
    выходит сразу: если ограничения уже нет, проверка не проходит.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='social_articles'"
    ).fetchone()
    if not row or "group_id TEXT NOT NULL" not in (row["sql"] or ""):
        return
    logger.info("Пересборка social_articles: снимается NOT NULL с group_id")
    conn.execute("PRAGMA foreign_keys=OFF")
    old = conn.execute(
        "SELECT COUNT(*) FROM social_articles").fetchone()[0]
    conn.execute("ALTER TABLE social_articles RENAME TO social_articles_old")
    conn.execute("""
        CREATE TABLE social_articles (
            id TEXT PRIMARY KEY,
            group_id TEXT,
            channel_id TEXT DEFAULT '',
            author_id TEXT NOT NULL,
            title TEXT NOT NULL,
            excerpt TEXT DEFAULT '',
            cover_image TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            views_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (group_id) REFERENCES social_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
        )""")
    # channel_id у переименованной таблицы может ещё не существовать,
    # поэтому он не переносится и остаётся пустым.
    conn.execute("""
        INSERT INTO social_articles
            (id, group_id, author_id, title, excerpt, cover_image, status,
             views_count, created_at, updated_at)
        SELECT id, group_id, author_id, title, excerpt, cover_image, status,
               views_count, created_at, updated_at
        FROM social_articles_old""")
    conn.execute("DROP TABLE social_articles_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_group "
                 "ON social_articles(group_id, status, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_channel "
                 "ON social_articles(channel_id, status, updated_at DESC)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    logger.info("social_articles пересобрана, статей перенесено: %d", old)


def _relax_comments_post_id(conn):
    """Убрать FK с social_comments.post_id.

    Комментарии полиморфные: посты профиля используют post_id, а посты
    групп/каналов используют target_type/target_id. FK на social_posts ломает
    комментарии групп. ALTER TABLE в SQLite не умеет менять ограничения,
    поэтому таблицу пересоздаем и переносим данные.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='social_comments'"
    ).fetchone()
    if not row or "FOREIGN KEY (post_id) REFERENCES social_posts" not in (row["sql"] or ""):
        return

    logger.info("Пересборка social_comments: снимается FK с post_id")
    conn.execute("PRAGMA foreign_keys=OFF")
    old_count = conn.execute("SELECT COUNT(*) FROM social_comments").fetchone()[0]
    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(social_comments)").fetchall()]

    conn.execute("ALTER TABLE social_comments RENAME TO social_comments_old")
    conn.execute("""
        CREATE TABLE social_comments (
            id TEXT PRIMARY KEY,
            post_id TEXT,
            author_id TEXT NOT NULL,
            parent_id TEXT DEFAULT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL DEFAULT 0,
            likes_count INTEGER DEFAULT 0,
            target_type TEXT DEFAULT 'post',
            target_id TEXT DEFAULT '',
            FOREIGN KEY (author_id) REFERENCES social_users(profile_id)
        )""")

    new_cols = {
        "id", "post_id", "author_id", "parent_id", "content",
        "created_at", "updated_at", "likes_count", "target_type", "target_id",
    }
    copy_cols = [c for c in old_cols if c in new_cols]
    if copy_cols:
        cols_sql = ", ".join(copy_cols)
        conn.execute(
            f"INSERT INTO social_comments ({cols_sql}) SELECT {cols_sql} FROM social_comments_old"
        )

    conn.execute("DROP TABLE social_comments_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_post "
                 "ON social_comments(post_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_target "
                 "ON social_comments(target_type, target_id, created_at)")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    logger.info("social_comments пересобрана, комментариев перенесено: %d", old_count)


def _ensure_user(profile_id: str, display_name: str = ""):
    for attempt in range(3):
        try:
            conn = _get_db()
            cur = conn.execute("SELECT profile_id FROM social_users WHERE profile_id = ?", (profile_id,))
            if cur.fetchone() is None:
                conn.execute(
                    "INSERT INTO social_users (profile_id, display_name, joined_at) VALUES (?, ?, ?)",
                    (profile_id, display_name or profile_id, time.time())
                )
                conn.commit()
            return
        except sqlite3.OperationalError as e:
            if attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise

def _get_active_profile(request: Request) -> str:
    """Кто вызывает соцсеть: cookie профиля → state → общедоступный.

    Глобальный активный профиль раньше подставлялся здесь, хотя он один на
    сервер и меняется навигацией любого браузера. Из-за этого при двух
    открытых профилях сообщения отправлялись и читались от имени того, кто
    последним открыл страницу (чужие переписки и уведомления).
    """
    try:
        import profiles_module as PM
        value = request.cookies.get(PM.COOKIE_NAME, "") or ""
        if value:
            pid = PM.resolve_profile_id(value)
            if pid:
                return pid
    except Exception:
        swallow.note('social_module._get_active_profile:L1307')
        pass
    active = getattr(request.state, "profile_id", None)
    if active:
        return active
    try:
        import profiles_module as PM
        return PM.resolve_profile_id("") or "default"
    except Exception:
        swallow.note('social_module._get_active_profile:L1316')
        return "default"

def _parse_hashtags(content: str, post_id: str, conn):
    tags = set(re.findall(r'#(\w[\w-]*)', content))
    now = time.time()
    for tag in tags:
        if len(tag) > 50:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO social_hashtags (id, tag, post_id, created_at) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), tag.lower(), post_id, now)
        )

_MENTION_RE = re.compile(
    r'@([^\s@,.:;!?()\[\]{}"\']+(?:[ ][^\s@,.:;!?()\[\]{}"\']+)?)')

def _resolve_mention(conn, raw: str):
    """Найти пользователя по упоминанию.

    display_name может содержать пробелы («Иван Петров»), поэтому одной
    регулярки @(\\w[\\w.]*) мало — @Иван не находил «Иван Петров».
    Сначала пробуем одно слово (частый случай), затем два — по границам
    текста, чтобы не прихватить следующее слово сообщения.

    Сравнение регистронезависимое: пользователь набирает @иван, а в профиле
    «Иван», и раньше такое упоминание молча терялось.
    """
    raw = raw.strip()
    if not raw or len(raw) > 50:
        return None
    words = raw.split()
    for count in (2, 1):
        if len(words) < count:
            continue
        candidate = " ".join(words[:count])
        user = conn.execute(
            "SELECT profile_id FROM social_users "
            "WHERE display_name = ? COLLATE NOCASE OR profile_id = ?",
            (candidate, candidate)
        ).fetchone()
        if user:
            return user, candidate
    return None

def _parse_mentions(content: str, post_id: str, author_id: str, conn,
                    target_type: str = "post", target_id: str = None):
    """Разобрать @упоминания и записать их вместе с уведомлениями.

    Раньше функция вызывалась только из маршрутов постов ленты: в
    комментариях, ответах форума и постах групп упоминания не работали
    вовсе, хотя колонка social_mentions.comment_id для них и была заведена.
    Теперь target_type/target_id задают, что именно упомянули.
    """
    now = time.time()
    seen = set()
    resolved_id = target_id if target_id is not None else post_id
    for raw in _MENTION_RE.findall(content or ""):
        resolved = _resolve_mention(conn, raw)
        if not resolved:
            continue
        user, username = resolved
        # Автор не уведомляет сам себя: упоминание себя в посте не смысла.
        if user["profile_id"] == author_id:
            continue
        key = (user["profile_id"], username.lower())
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO social_mentions (id, username, post_id, author_id, mentioned_id, created_at) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), username, post_id, author_id, user["profile_id"], now)
        )
        _create_notification(conn, user["profile_id"], "mention", author_id, target_type, resolved_id)
    return len(seen)

@router.get("/mention/suggest")
def mention_suggest(request: Request, q: str = "", group_id: str = "",
                          limit: int = 8):
    """Подсказки для упоминаний.

    Раньше автодополнения не существовало вовсе: упоминание можно было
    только вписать руками, и оно находилось по точному совпадению имени.

    Кандидаты берутся из состава группы (если указана) — упоминать имеет
    смысл тех, кто рядом в контексте, — иначе из всех пользователей.
    Сначала идут совпадения с начала строки, потом вхождения в середине.
    """
    viewer = _get_active_profile(request)
    conn = _get_db()
    q = (q or "").strip().lstrip("@")
    limit = max(1, min(int(limit or 8), 20))
    rows = []
    if group_id:
        rows = conn.execute(
            "SELECT u.profile_id, u.display_name, u.avatar_path, u.avatar_crop "
            "FROM social_group_members m JOIN social_users u ON u.profile_id = m.profile_id "
            "WHERE m.group_id=? AND u.profile_id!=? ORDER BY u.display_name LIMIT 60",
            (group_id, viewer)
        ).fetchall()
    if not rows:
        if q:
            rows = conn.execute(
                "SELECT profile_id, display_name, avatar_path, avatar_crop FROM social_users "
                "WHERE profile_id != ? AND (display_name LIKE ? OR profile_id LIKE ?) "
                "ORDER BY display_name LIMIT 60",
                (viewer, "%" + q + "%", "%" + q + "%")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT profile_id, display_name, avatar_path, avatar_crop FROM social_users "
                "WHERE profile_id != ? ORDER BY last_seen DESC LIMIT 60", (viewer,)
            ).fetchall()
    low = q.lower()
    picked = [r for r in rows if (r["display_name"] or "").lower().startswith(low)] if low else list(rows)
    if low:
        picked += [r for r in rows
                   if (r["display_name"] or "").lower().startswith(low) is False
                   and low in (r["display_name"] or "").lower()]
    # Из уже отобранных оставляем те, где есть совпадение (если искали).
    if low:
        picked = [r for r in picked if low in ((r["display_name"] or "") + " " + r["profile_id"]).lower()]
    out = []
    for r in picked[:limit]:
        out.append({
            "profile_id": r["profile_id"],
            "display_name": r["display_name"] or r["profile_id"],
            "avatar_path": r["avatar_path"] or "",
            "avatar_crop": r["avatar_crop"] or "",
        })
    return JSONResponse({"users": out})

def _json_serialize(obj):
    if isinstance(obj, (sqlite3.Row, dict)):
        return {k: v for k, v in obj.items()}
    return obj

def _safe_attachments(raw) -> str:
    """Проверяет, что attachments — это JSON-массив объектов.

    Раньше строка из тела запроса уходила в БД как есть, и любой клиент
    мог положить туда что угодно (включая не-JSON), после чего все
    места с JSON.parse(attachments) на клиенте падали.
    """
    try:
        data = json.loads(raw or "[]")
    except Exception:
        raise HTTPException(400, "Некорректные вложения")
    if not isinstance(data, list):
        raise HTTPException(400, "Некорректные вложения")
    if len(data) > 20:
        raise HTTPException(400, "Слишком много вложений (максимум 20)")
    out = []
    for item in data:
        if not isinstance(item, dict):
            raise HTTPException(400, "Некорректные вложения")
        url = str(item.get("url") or item.get("src") or item.get("path") or "")
        # Разрешены только пути, выданные нашим же загрузчиками
        # (/api/social/file/...) и статика (/static/... — стикеры, эмодзи).
        # Внешний URL раньше проходил как есть и рендерился в <img src>.
        if url and not (url.startswith("/api/social/") or url.startswith("/static/") or url.startswith("/api/media/")):
            raise HTTPException(400, "Недопустимая ссылка на вложение")
        out.append(item)
    return json.dumps(out, ensure_ascii=False)

def _laya_triage_content(content: str):
    """Мягкий триаж текста движком laya (спам/тон/срочность).

    Возвращает вердикт или None, если движок выключен/недоступен. Никаких
    исключений наружу: по умолчанию это «подсказка», а не запрет.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('social_module._laya_triage_content:L1490')
        return None
    if not laya_client.feature("social_triage"):
        return None
    try:
        return laya_client.triage_social(content or "")
    except Exception as e:
        logger.debug(f"laya triage failed: {e}")
        return None


def _laya_block_spam():
    """Запрещать ли посты, которые laya назвал спамом (по умолчанию — нет)."""
    try:
        import config_module
        sec = (config_module.get_config() or {}).get("laya") or {}
        return bool(sec.get("social_triage_block_spam"))
    except Exception:
        swallow.note('social_module._laya_block_spam:L1508')
        return False


def _store_laya_triage(conn, table: str, row_id: str, verdict):
    """Сохранить вердикт триажа рядом с записью (если колонка есть)."""
    if not verdict:
        return
    try:
        conn.execute(f"UPDATE {table} SET laya_triage=? WHERE id=?",
                     (json.dumps(verdict, ensure_ascii=False), row_id))
    except Exception as e:
        logger.debug(f"laya triage not stored in {table}: {e}")


def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)

def _now():
    return time.time()

_ONLINE_PERIOD = 180.0

def _is_online_from_last_seen(last_seen):
    if not last_seen:
        return False
    return (time.time() - last_seen) < _ONLINE_PERIOD

try:
    _init_db()
except sqlite3.OperationalError as e:
    if logger:
        logger.warning(f"Social DB init error (module disabled): {e}")

def _seed_default_stickers():
    """Create built-in sticker packs (SVG emoji stickers). Idempotent: adds only missing packs."""
    try:
        conn = _get_db()
        packs = [
            ("sticker-pack-smiles", "Смайлы", ["😀","😂","🥰","😎","🤩","😭","🥳","🤯","😴","🤗","🙃","😇","😊","😍","😘","😋","😜","🤪","🥺","😅","🤣","😆","😁","🙂","😉","😌","😏","😒","🙄","😬","😱","🥵","🥶","🤒","🤯","🤑","🤠","😈","👻","💀"]),
            ("sticker-pack-animals", "Животные", ["🐶","🐱","🦊","🐼","🐸","🐨","🦄","🐷","🐰","🦁","🐙","🐳","🐻","🦊","🐯","🐮","🐔","🐧","🐦","🐺","🐴","🐝","🦋","🐢","🐍","🦖","🦕","🐘","🦒","🦘","🐪","🦩","🦚","🦜","🦃","🦢","🦭","🐬","🦈","🐊"]),
            ("sticker-pack-food", "Еда и напитки", ["🍕","🍔","🍩","🍦","🍓","🍉","🍣","🌮","🍰","🥤","🍪","🍇","🍏","🍎","🍐","🍊","🍋","🍌","🍒","🍑","🥭","🍍","🥥","🥝","🍅","🥑","🍆","🥦","🌶️","🌽","🥕","☕","🍵","🧃","🍺","🍻","🥂","🍷","🧁","🎂"]),
            ("sticker-pack-hearts", "Сердца и эмоции", ["❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔","❣️","💕","💞","💓","💗","💖","💘","💝","💟","♥️","💌","💋","💯","💢","💥","💫","💦","💨","💬","💭","💤","💮","💝","🩷","🩶","🩵"]),
            ("sticker-pack-party", "Праздник", ["🎉","🎊","🎈","🎁","🎀","✨","🌟","⭐","🥳","🎂","🍾","🍻","🥂","🎆","🎇","🧨","🪅","🪆","🎃","🦃","🎄","🎅","🤶","☃️","🎵","🎶","🏆","🥇","🥈","🥉","🏅","🎖️","🎗️","🎫","🎟️","🎪","🎭","🎬","🎤","🎧"]),
            ("sticker-pack-hands", "Жесты и руки", ["👍","👎","👌","✌️","🤞","🤟","🤘","🤙","👏","🙌","🙏","💪","🤝","🫶","✊","👊","🤛","🤜","👋","🤚","🖐️","✋","🖖","☝️","👈","👉","👆","👇","🐣","🫰","🤌","🤏","🤳"]),
            ("sticker-pack-cats", "Котики", ["😺","😸","😹","😻","😼","😽","🙀","😿","😾","🐱","🐈","🐈‍⬛","🐾","💤","🍴","🎣","🎀","❤️","💙","💚","💛","🧡","💜","🖤","🤍","🤎","😍","🥰","😘","😎","🤩","😭","🥳","😴","🤗","🙃","😇"]),
            ("sticker-pack-magic", "Магия и космос", ["✨","💫","⭐","🌟","⚡","☄️","🔥","🌈","🌙","☀️","🌊","🌪️","🌀","❄️","☃️","🌍","🌎","🌏","🪐","🚀","🛸","👽","🤖","👾","💥","🔮","🧿","🔭","⚗️","🧪","🧫","🧬","🔬","💡","🔋","🫧","🌹","💐"]),
        ]
        gradients = [
            ("#ffd166", "#f471b5"), ("#7f9cf5", "#a78bfa"), ("#34d399", "#22d3ee"),
            ("#fbbf24", "#f97316"), ("#f472b6", "#ec4899"), ("#a3e635", "#22c55e"),
            ("#f87171", "#ef4444"), ("#38bdf8", "#6366f1"), ("#34d399", "#0ea5e9"),
            ("#fbbf24", "#eab308"), ("#e879f9", "#a855f7"), ("#fca5a5", "#f472b6"),
        ]
        now = _now()
        for pack_id, name, emojis in packs:
            exists = conn.execute("SELECT 1 FROM social_sticker_packs WHERE id=? AND is_default=1", (pack_id,)).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO social_sticker_packs (id, owner_id, name, is_default, created_at) VALUES (?,?,?,1,?)",
                (pack_id, "system", name, now)
            )
            for i, emo in enumerate(emojis):
                g = gradients[i % len(gradients)]
                fname = f"{pack_id}_{i}.svg"
                path = os.path.join(STICKERS_DIR, fname)
                svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs><radialGradient id="g" cx="50%" cy="35%" r="80%">
    <stop offset="0%" stop-color="{g[0]}"/><stop offset="100%" stop-color="{g[1]}"/>
  </radialGradient></defs>
  <rect width="256" height="256" rx="56" fill="url(#g)"/>
  <text x="128" y="158" font-size="120" text-anchor="middle">{emo}</text>
</svg>'''
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(svg)
                except Exception as e:
                    logger.warning(f"Sticker seed error: {e}")
                    continue
                conn.execute(
                    "INSERT INTO social_stickers (id, pack_id, url, emoji, sort_order, created_at) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), pack_id, f"/api/social/file/stickers/{fname}", emo, i, now)
                )
        conn.commit()
        logger.info("Seeded default sticker packs")
    except Exception as e:
        logger.warning(f"Sticker seed failed: {e}")

_seed_default_stickers()
# --- Helper: get profile info from profiles_module ---

def _get_profile_name(profile_id: str) -> str:
    try:
        import profiles_module
        config = profiles_module.get_profile_config(profile_id)
        if config:
            return config.get("profile_name", profile_id)
    except ImportError:
        swallow.note('social_module._get_profile_name:L1609')
        pass
    return profile_id

# --- Profiles API ---

@router.get("/me")
async def get_my_profile(request: Request):
    profile_id = _get_active_profile(request)
    return await get_profile_endpoint(profile_id, request)

@router.get("/profile/{profile_id}")
async def get_profile(profile_id: str, request: Request):
    return await get_profile_endpoint(profile_id, request)

async def get_profile_endpoint(profile_id: str, request: Request = None):
    _ensure_user(profile_id, _get_profile_name(profile_id))
    conn = _get_db()
    row = conn.execute("SELECT * FROM social_users WHERE profile_id = ?", (profile_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    data = _row_to_dict(row)
    data["display_name"] = data["display_name"] or _get_profile_name(profile_id)

    # Parse avatar crop params
    try:
        data["avatar_crop"] = json.loads(data.get("avatar_crop") or '{"zoom":1,"x":0.5,"y":0.5}')
    except Exception:
        swallow.note('social_module.get_profile_endpoint:L1637')
        data["avatar_crop"] = {"zoom": 1, "x": 0.5, "y": 0.5}

    # Avatars gallery (multiple avatars + flip)
    av_rows = conn.execute(
        "SELECT id, path, crop, is_active, created_at FROM social_avatars WHERE profile_id=? ORDER BY is_active DESC, sort_order, created_at",
        (profile_id,)
    ).fetchall()
    data["avatars"] = []
    for a in av_rows:
        ad = _row_to_dict(a)
        try:
            ad["crop"] = json.loads(ad.get("crop") or '{"zoom":1,"x":0.5,"y":0.5}')
        except Exception:
            swallow.note('social_module.get_profile_endpoint:L1651')
            ad["crop"] = {"zoom": 1, "x": 0.5, "y": 0.5}
        data["avatars"].append(ad)
    
    # Profile-level privacy check
    my_profile_id = _get_active_profile(request) if request else None
    is_me = my_profile_id == profile_id
    profile_privacy = data.get("profile_privacy", 0)
    if profile_privacy == 2 and not is_me:
        raise HTTPException(403, "Профиль приватный")
    if profile_privacy == 1 and not is_me and my_profile_id:
        is_friend = conn.execute(
            "SELECT 1 FROM social_friendships WHERE ((requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)) AND status='accepted'",
            (my_profile_id, profile_id, profile_id, my_profile_id)
        ).fetchone()
        if not is_friend:
            raise HTTPException(403, "Профиль доступен только друзьям")
    
    # Load profile extras
    extras = conn.execute("SELECT * FROM social_profile_extras WHERE profile_id=?", (profile_id,)).fetchall()
    extra_dict = {}
    privacy_dict = {}
    for e in extras:
        extra_dict[e["field_key"]] = e["field_value"]
        privacy_dict[e["field_key"]] = e["privacy"]
    data["extras"] = extra_dict
    data["extras_privacy"] = privacy_dict
    
    # Load photos. Фото из альбомов (origin='album') сюда не попадают:
    # они относятся к группе или каналу, а не к профилю человека.
    photos = conn.execute(
        "SELECT * FROM social_profile_photos WHERE profile_id=? "
        "AND (origin IS NULL OR origin='profile') "
        "ORDER BY created_at DESC LIMIT 50", (profile_id,)).fetchall()
    data["photos"] = [_row_to_dict(p) for p in photos]
    
    # Load privacy settings
    try:
        data["privacy_settings"] = json.loads(data.get("privacy_settings", "{}"))
    except Exception:
        swallow.note('social_module.get_profile_endpoint:L1691')
        data["privacy_settings"] = {}
    
    # Filter by field-level privacy
    if not is_me:
        ps = data.get("privacy_settings", {}) or {}
        for key in list(extra_dict.keys()):
            field_privacy = privacy_dict.get(key, 0)
            if field_privacy > 0 and not is_me:
                # 0=public, 1=friends, 2=only_me
                if field_privacy == 2:
                    del extra_dict[key]
                elif field_privacy == 1 and my_profile_id:
                    is_friend = conn.execute(
                        "SELECT 1 FROM social_friendships WHERE ((requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)) AND status='accepted'",
                        (my_profile_id, profile_id, profile_id, my_profile_id)
                    ).fetchone()
                    if not is_friend:
                        del extra_dict[key]
    
    if is_me:
        conn.execute("UPDATE social_users SET last_seen = ?, is_online = 1 WHERE profile_id = ?", (_now(), profile_id))
    else:
        data["is_online"] = _is_online_from_last_seen(data.get("last_seen"))
    conn.commit()
    friend_count = conn.execute(
        "SELECT COUNT(*) FROM social_friendships WHERE (requester_id=? OR addressee_id=?) AND status='accepted'",
        (profile_id, profile_id)
    ).fetchone()[0]
    post_count = conn.execute("SELECT COUNT(*) FROM social_posts WHERE author_id=?", (profile_id,)).fetchone()[0]
    data["friends_count"] = friend_count
    data["posts_count"] = post_count
    return JSONResponse(data)

@router.put("/profile")
def update_profile(data: ProfileUpdate, request: Request):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    fields = data.dict(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Нет полей для обновления")
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [profile_id]
    conn = _get_db()
    conn.execute(f"UPDATE social_users SET {sets} WHERE profile_id=?", vals)
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/profile/{profile_id}/posts")
def get_profile_posts(profile_id: str, offset: int = Query(0), limit: int = Query(20)):
    conn = _get_db()
    rows = conn.execute(
        "SELECT p.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_posts p "
        "JOIN social_users u ON p.author_id = u.profile_id "
        "WHERE p.author_id = ? ORDER BY p.is_pinned DESC, p.created_at DESC LIMIT ? OFFSET ?",
        (profile_id, limit, offset)
    ).fetchall()
    posts = []
    for r in rows:
        post = _row_to_dict(r)
        if post.get("repost_id"):
            orig = conn.execute(
                "SELECT p.content, u.display_name, u.profile_id FROM social_posts p "
                "JOIN social_users u ON p.author_id = u.profile_id WHERE p.id=?",
                (post["repost_id"],)
            ).fetchone()
            if orig:
                post["repost_author_name"] = orig["display_name"] or orig["profile_id"]
                post["repost_content"] = orig["content"]
        post["liked_by_me"] = False
        posts.append(post)
    return JSONResponse(posts)

# --- Posts API ---

@router.post("/posts")
def create_post(data: PostCreate, request: Request):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    if not _check_rate_limit(f"post:{profile_id}", 10, 60):
        raise HTTPException(429, "Слишком много постов. Подождите.")
    content = (data.content or "").strip()
    attachments = _safe_attachments(data.attachments)
    if not content and attachments == "[]":
        raise HTTPException(400, "Текст обязателен")
    # Мягкий триаж (laya) — по умолчанию только подсказка, пост не блокируется.
    _tri = _laya_triage_content(content)
    if _tri and _tri.get("kind") == "spam" and _laya_block_spam():
        raise HTTPException(422, "Сообщение отклонено: похоже на спам")
    post_id = str(uuid.uuid4())
    now = _now()
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_posts (id, author_id, content, created_at, privacy, attachments) VALUES (?,?,?,?,?,?)",
        (post_id, profile_id, content, now, data.privacy, attachments)
    )
    _store_laya_triage(conn, "social_posts", post_id, _tri)
    # Add to own feed
    conn.execute(
        "INSERT INTO social_feeds (id, user_id, post_id, author_id, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), profile_id, post_id, profile_id, now)
    )
    # Distribute to friends' feeds
    friend_ids = conn.execute(
        "SELECT requester_id, addressee_id FROM social_friendships WHERE status='accepted' AND (requester_id=? OR addressee_id=?)",
        (profile_id, profile_id)
    ).fetchall()
    for f in friend_ids:
        friend_id = f["requester_id"] if f["addressee_id"] == profile_id else f["addressee_id"]
        conn.execute(
            "INSERT INTO social_feeds (id, user_id, post_id, author_id, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), friend_id, post_id, profile_id, now)
        )
    _parse_hashtags(data.content, post_id, conn)
    _parse_mentions(data.content, post_id, profile_id, conn)
    conn.commit()
    # Federate to ActivityPub
    try:
        from activitypub_module import federate_post
        federate_post(profile_id, post_id, data.content)
    except Exception as e:
        logger.debug(f"Federation error (create_post): {e}")
    return JSONResponse({"id": post_id, "ok": True})

@router.get("/posts/hashtag/{tag}")
def get_posts_by_hashtag(tag: str, request: Request, offset: int = Query(0), limit: int = Query(30)):
    conn = _get_db()
    rows = conn.execute(
        "SELECT p.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_online FROM social_posts p "
        "JOIN social_hashtags h ON h.post_id = p.id "
        "JOIN social_users u ON p.author_id = u.profile_id "
        "WHERE h.tag = ? ORDER BY h.created_at DESC LIMIT ? OFFSET ?",
        (tag.lower(), limit, offset)
    ).fetchall()
    posts = []
    for r in rows:
        post = _row_to_dict(r)
        post["liked_by_me"] = False
        posts.append(post)
    return JSONResponse(posts)

@router.delete("/posts/{post_id}")
def delete_post(post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT author_id FROM social_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Пост не найден")
    if row["author_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    conn.execute("DELETE FROM social_comments WHERE post_id=?", (post_id,))
    conn.execute("DELETE FROM social_posts WHERE id=?", (post_id,))
    conn.execute("DELETE FROM social_feeds WHERE post_id=?", (post_id,))
    conn.commit()
    # Federate delete to ActivityPub
    try:
        from activitypub_module import federate_delete_post
        federate_delete_post(profile_id, post_id)
    except Exception as e:
        logger.debug(f"Federation error (delete_post): {e}")
    return JSONResponse({"ok": True})

@router.put("/posts/{post_id}")
def update_post(post_id: str, request: Request, data: dict = Body(...)):
    """Редактирование своего поста на стене."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT author_id FROM social_posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Пост не найден")
    if row["author_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    content = (data.get("content") or "").strip()
    attachments = data.get("attachments")
    if content:
        conn.execute("UPDATE social_posts SET content=?, updated_at=? WHERE id=?", (content, _now(), post_id))
        _parse_hashtags(content, post_id, conn)
    if attachments is not None:
        if isinstance(attachments, list):
            attachments = json.dumps(attachments)
        conn.execute("UPDATE social_posts SET attachments=? WHERE id=?", (attachments, post_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/posts/{post_id}/like")
def toggle_like(post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"like:{profile_id}", 30, 60):
        raise HTTPException(429, "Слишком много лайков.")
    conn = _get_db()
    row = conn.execute("SELECT id FROM social_likes WHERE user_id=? AND target_type='post' AND target_id=?", (profile_id, post_id)).fetchone()
    if row:
        conn.execute("DELETE FROM social_likes WHERE id=?", (row["id"],))
        conn.execute("UPDATE social_posts SET likes_count = MAX(0, likes_count - 1) WHERE id=?", (post_id,))
        conn.commit()
        try:
            import activitypub_module
            activitypub_module.federate_like(profile_id, post_id, like=False)
        except Exception:
            swallow.note('social_module.toggle_like:L1890')
            pass
        return JSONResponse({"liked": False})
    conn.execute(
        "INSERT INTO social_likes (id, user_id, target_type, target_id, created_at) VALUES (?,?,'post',?,?)",
        (str(uuid.uuid4()), profile_id, post_id, _now())
    )
    conn.execute("UPDATE social_posts SET likes_count = likes_count + 1 WHERE id=?", (post_id,))
    post = conn.execute("SELECT author_id FROM social_posts WHERE id=?", (post_id,)).fetchone()
    if post and post["author_id"] != profile_id:
        _create_notification(conn, post["author_id"], "like", profile_id, "post", post_id)
    conn.commit()
    try:
        import activitypub_module
        activitypub_module.federate_like(profile_id, post_id, like=True)
    except Exception:
        swallow.note('social_module.toggle_like:L1906')
        pass
    return JSONResponse({"liked": True})

@router.get("/posts/{post_id}/comments")
def get_comments(post_id: str):
    conn = _get_db()
    rows = conn.execute(
        "SELECT c.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_comments c "
        "JOIN social_users u ON c.author_id = u.profile_id "
        "WHERE c.post_id = ? ORDER BY c.created_at ASC", (post_id,)
    ).fetchall()
    comments = [_row_to_dict(r) for r in rows]
    # Build nested structure
    comment_map = {}
    root_comments = []
    for c in comments:
        c["replies"] = []
        comment_map[c["id"]] = c
    for c in comments:
        if c["parent_id"] and c["parent_id"] in comment_map:
            comment_map[c["parent_id"]]["replies"].append(c)
        else:
            root_comments.append(c)
    return JSONResponse(root_comments)

@router.post("/posts/{post_id}/comments")
def add_comment(post_id: str, data: CommentCreate, request: Request):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    if not _check_rate_limit(f"comment:{profile_id}", 10, 60):
        raise HTTPException(429, "Слишком много комментариев.")
    comment_id = str(uuid.uuid4())
    now = _now()
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_comments (id, post_id, author_id, parent_id, content, created_at) VALUES (?,?,?,?,?,?)",
        (comment_id, post_id, profile_id, data.parent_id, data.content, now)
    )
    _store_laya_triage(conn, "social_comments", comment_id, _laya_triage_content(data.content or ""))
    conn.execute("UPDATE social_posts SET comments_count = comments_count + 1 WHERE id=?", (post_id,))
    post = conn.execute("SELECT author_id FROM social_posts WHERE id=?", (post_id,)).fetchone()
    if post and post["author_id"] != profile_id:
        _create_notification(conn, post["author_id"], "comment", profile_id, "post", post_id)
    conn.commit()
    return JSONResponse({"id": comment_id, "ok": True})

@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT author_id, post_id FROM social_comments WHERE id=?", (comment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Комментарий не найден")
    if row["author_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    conn.execute("DELETE FROM social_comments WHERE id=?", (comment_id,))
    conn.execute("UPDATE social_posts SET comments_count = MAX(0, comments_count - 1) WHERE id=?", (row["post_id"],))
    conn.commit()
    return JSONResponse({"ok": True})

@router.put("/comments/{comment_id}")
def update_comment(comment_id: str, request: Request, data: dict = Body(...)):
    """Редактирование своего комментария."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT author_id, post_id FROM social_comments WHERE id=?", (comment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Комментарий не найден")
    if row["author_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Пустой комментарий")
    conn.execute("UPDATE social_comments SET content=?, updated_at=? WHERE id=?", (content, _now(), comment_id))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Friends API ---

@router.get("/friends")
def get_friends(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT f.*, u.display_name, u.avatar_path, u.avatar_crop, u.status, u.is_online, u.last_seen "
        "FROM social_friendships f JOIN social_users u ON "
        "(CASE WHEN f.requester_id=? THEN f.addressee_id ELSE f.requester_id END) = u.profile_id "
        "WHERE (f.requester_id=? OR f.addressee_id=?) AND f.status='accepted'",
        (profile_id, profile_id, profile_id)
    ).fetchall()
    friends = []
    for r in rows:
        fd = _row_to_dict(r)
        if fd.get("last_seen"):
            fd["is_online"] = _is_online_from_last_seen(fd.get("last_seen"))
        else:
            fd["is_online"] = bool(fd.get("is_online"))
        friends.append(fd)
    return JSONResponse(friends)

@router.get("/friends/requests")
def get_friend_requests(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    incoming = conn.execute(
        "SELECT f.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_friendships f "
        "JOIN social_users u ON f.requester_id = u.profile_id "
        "WHERE f.addressee_id=? AND f.status='pending'", (profile_id,)
    ).fetchall()
    outgoing = conn.execute(
        "SELECT f.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_friendships f "
        "JOIN social_users u ON f.addressee_id = u.profile_id "
        "WHERE f.requester_id=? AND f.status='pending'", (profile_id,)
    ).fetchall()
    return JSONResponse({
        "incoming": [_row_to_dict(r) for r in incoming],
        "outgoing": [_row_to_dict(r) for r in outgoing]
    })

@router.post("/friends/request")
def send_friend_request(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"friend_req:{profile_id}", 10, 60):
        raise HTTPException(429, "Слишком много запросов в друзья.")
    target_id = data.get("profile_id")
    if not target_id or target_id == profile_id:
        raise HTTPException(400, "Некорректный запрос")
    _ensure_user(target_id)
    conn = _get_db()
    existing = conn.execute(
        "SELECT * FROM social_friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
        (profile_id, target_id, target_id, profile_id)
    ).fetchone()
    if existing:
        if existing["status"] == "blocked":
            raise HTTPException(403, "Действие заблокировано")
        if existing["status"] == "pending":
            return JSONResponse({"ok": True, "status": "pending"})
        if existing["status"] == "accepted":
            return JSONResponse({"ok": True, "status": "accepted"})
    conn.execute(
        "INSERT INTO social_friendships (id, requester_id, addressee_id, status, created_at) VALUES (?,?,?,'pending',?)",
        (str(uuid.uuid4()), profile_id, target_id, _now())
    )
    _create_notification(conn, target_id, "friend_request", profile_id, "friendship", "")
    conn.commit()
    return JSONResponse({"ok": True, "status": "requested"})

@router.post("/friends/accept")
def accept_friend_request(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    requester_id = data.get("profile_id")
    if not requester_id:
        raise HTTPException(400, "Некорректный запрос")
    conn = _get_db()
    row = conn.execute(
        "SELECT id FROM social_friendships WHERE requester_id=? AND addressee_id=? AND status='pending'",
        (requester_id, profile_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    conn.execute("UPDATE social_friendships SET status='accepted', updated_at=? WHERE id=?", (_now(), row["id"]))
    # Add past posts to feed
    now = _now()
    posts = conn.execute(
        "SELECT id, author_id FROM social_posts WHERE author_id=? ORDER BY created_at DESC LIMIT 50",
        (requester_id,)
    ).fetchall()
    for p in posts:
        conn.execute(
            "INSERT OR IGNORE INTO social_feeds (id, user_id, post_id, author_id, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), profile_id, p["id"], p["author_id"], now)
        )
    posts2 = conn.execute(
        "SELECT id, author_id FROM social_posts WHERE author_id=? ORDER BY created_at DESC LIMIT 50",
        (profile_id,)
    ).fetchall()
    for p in posts2:
        conn.execute(
            "INSERT OR IGNORE INTO social_feeds (id, user_id, post_id, author_id, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), requester_id, p["id"], p["author_id"], now)
        )
    _create_notification(conn, requester_id, "friend_accept", profile_id, "friendship", "")
    conn.commit()
    return JSONResponse({"ok": True, "status": "accepted"})

@router.post("/friends/reject")
def reject_friend_request(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    requester_id = data.get("profile_id")
    conn = _get_db()
    conn.execute(
        "DELETE FROM social_friendships WHERE requester_id=? AND addressee_id=? AND status='pending'",
        (requester_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/friends/block")
async def block_user(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target_id = data.get("profile_id")
    if not target_id or target_id == profile_id:
        raise HTTPException(400, "Некорректный запрос")
    conn = _get_db()
    conn.execute(
        "DELETE FROM social_friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
        (profile_id, target_id, target_id, profile_id)
    )
    conn.execute(
        "INSERT OR REPLACE INTO social_friendships (id, requester_id, addressee_id, status, created_at, updated_at) VALUES (?,?,?,'blocked',?,?)",
        (str(uuid.uuid4()), profile_id, target_id, _now(), _now())
    )
    conn.execute("DELETE FROM social_feeds WHERE user_id=? AND author_id=?", (profile_id, target_id))
    conn.execute("DELETE FROM social_feeds WHERE user_id=? AND author_id=?", (target_id, profile_id))
    conn.commit()
    try:
        # Если блокируется федеративный пользователь — отписаться (Undo Follow)
        import activitypub_module as apm
        actor_url = conn.execute(
            "SELECT ap_actor_url FROM social_users WHERE profile_id=?", (target_id,)
        ).fetchone()
        if actor_url and actor_url["ap_actor_url"]:
            apm.federate_unfollow(profile_id, actor_url["ap_actor_url"])
            conn.execute("DELETE FROM social_remote_follows WHERE profile_id=? AND actor_url=?", (profile_id, actor_url["ap_actor_url"]))
            conn.commit()
    except Exception:
        swallow.note('social_module.block_user:L2134')
        pass
    try:
        if target_id in manager.all_connections:
            await manager.send_to_profile(target_id, {"type": "blocked_by", "profile_id": profile_id})
    except Exception:
        swallow.note('social_module.block_user:L2140')
        pass
    return JSONResponse({"ok": True})

@router.post("/friends/unblock")
def unblock_user(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target_id = data.get("profile_id")
    conn = _get_db()
    conn.execute(
        "DELETE FROM social_friendships WHERE requester_id=? AND addressee_id=? AND status='blocked'",
        (profile_id, target_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.delete("/friends/{friend_id}")
def remove_friend(friend_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute(
        "DELETE FROM social_friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
        (profile_id, friend_id, friend_id, profile_id)
    )
    # Remove from feeds
    conn.execute("DELETE FROM social_feeds WHERE user_id=? AND author_id=?", (profile_id, friend_id))
    conn.execute("DELETE FROM social_feeds WHERE user_id=? AND author_id=?", (friend_id, profile_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/friends/status/{target_id}")
def get_friend_status(target_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute(
        "SELECT status, requester_id FROM social_friendships WHERE (requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
        (profile_id, target_id, target_id, profile_id)
    ).fetchone()
    if not row:
        return JSONResponse({"status": "none"})
    result = {"status": row["status"]}
    if row["status"] == "pending":
        result["direction"] = "outgoing" if row["requester_id"] == profile_id else "incoming"
    return JSONResponse(result)

# --- Messages API ---

@router.get("/conversations")
def get_conversations(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    now = _now()
    # Исчезающие (ttl>0) просроченные сообщения не должны попадать ни в
    # превью диалога, ни в счётчик непрочитанных — иначе в списке чатов
    # висят бейджи и тексты, которых в переписке уже нет.
    exp = "(ttl IS NULL OR ttl=0 OR created_at+ttl > ?)"
    rows = conn.execute(
        f"SELECT c.*, cm.last_read_at, cm.is_pinned, cm.is_muted, cm.folder_id, cm.is_archived, "
        f"(SELECT content FROM social_messages m WHERE m.conversation_id=c.id AND {exp} ORDER BY created_at DESC LIMIT 1) as last_message, "
        f"(SELECT created_at FROM social_messages m WHERE m.conversation_id=c.id AND {exp} ORDER BY created_at DESC LIMIT 1) as last_message_at "
        f"FROM social_conversations c "
        f"JOIN social_conversation_members cm ON c.id = cm.conversation_id "
        f"WHERE cm.profile_id = ? "
        f"ORDER BY cm.is_pinned DESC, last_message_at DESC", [now, now, profile_id]
    ).fetchall()
    if not rows:
        return JSONResponse([])
    conv_ids = [r["id"] for r in rows]
    placeholders = ",".join(["?"] * len(conv_ids))
    all_members = conn.execute(
        f"SELECT cm.*, u.display_name, u.avatar_path, u.avatar_crop, u.last_seen, u.is_online "
        f"FROM social_conversation_members cm JOIN social_users u ON cm.profile_id = u.profile_id "
        f"WHERE cm.conversation_id IN ({placeholders})", conv_ids
    ).fetchall()
    members_by_conv = {}
    for m in all_members:
        cid = m["conversation_id"]
        md = _row_to_dict(m)
        if md.get("last_seen"):
            md["is_online"] = _is_online_from_last_seen(md.get("last_seen"))
        else:
            md["is_online"] = bool(md.get("is_online"))
        members_by_conv.setdefault(cid, []).append(md)
    unread_rows = conn.execute(
        f"SELECT m.conversation_id, COUNT(*) as cnt FROM social_messages m "
        f"JOIN social_conversation_members cm ON m.conversation_id = cm.conversation_id AND cm.profile_id = ? "
        f"WHERE m.conversation_id IN ({placeholders}) AND m.sender_id != ? "
        f"AND m.created_at > COALESCE(cm.last_read_at, 0) "
        f"AND (m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?) "
        f"GROUP BY m.conversation_id", [profile_id] + conv_ids + [profile_id, now]
    ).fetchall()
    unread_map = {r["conversation_id"]: r["cnt"] for r in unread_rows}
    result = []
    for r in rows:
        conv = _row_to_dict(r)
        conv["members"] = members_by_conv.get(conv["id"], [])
        conv["unread_count"] = unread_map.get(conv["id"], 0)
        result.append(conv)
    return JSONResponse(result)

@router.get("/search/global")
def global_search(request: Request, q: str = Query(""), limit: int = Query(20)):
    """Поиск по диалогам, сообщениям DM и сообщениям серверов (каналов), где состоит пользователь."""
    profile_id = _get_active_profile(request)
    query = (q or "").strip()
    if len(query) < 2:
        return JSONResponse({"conversations": [], "messages": [], "server_messages": []})
    conn = _get_db()
    like = f"%{query}%"
    limit = max(1, min(limit, 50))
    conversations = []
    conv_rows = conn.execute(
        "SELECT c.* FROM social_conversations c "
        "JOIN social_conversation_members cm ON c.id=cm.conversation_id "
        "WHERE cm.profile_id=? AND c.title LIKE ? "
        "ORDER BY c.created_at DESC LIMIT ?", (profile_id, like, limit)
    ).fetchall()
    for r in conv_rows:
        d = _row_to_dict(r)
        members = conn.execute(
            "SELECT cm.profile_id, u.display_name, u.avatar_path FROM social_conversation_members cm "
            "JOIN social_users u ON cm.profile_id=u.profile_id WHERE cm.conversation_id=?", (d["id"],)
        ).fetchall()
        d["members"] = [_row_to_dict(m) for m in members]
        conversations.append(d)
    messages = []
    msg_rows = conn.execute(
        "SELECT m.*, u.display_name, u.avatar_path FROM social_messages m "
        "JOIN social_conversation_members cm ON m.conversation_id=cm.conversation_id AND cm.profile_id=? "
        "JOIN social_users u ON m.sender_id=u.profile_id "
        "WHERE m.content LIKE ? AND (m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?) "
        "ORDER BY m.created_at DESC LIMIT ?", (profile_id, like, _now(), limit)
    ).fetchall()
    for r in msg_rows:
        d = _row_to_dict(r)
        conv = conn.execute(
            "SELECT c.title, c.is_group FROM social_conversations c WHERE c.id=?", (d["conversation_id"],)
        ).fetchone()
        d["conv_title"] = (conv["title"] if conv else "") or ""
        d["conv_is_group"] = conv["is_group"] if conv else 0
        messages.append(d)
    server_messages = []
    srv_rows = conn.execute(
        "SELECT m.*, u.display_name, u.avatar_path, u.is_bot, s.name AS server_name, s.id AS server_id, ch.name AS channel_name "
        "FROM social_server_messages m "
        "JOIN social_server_channels ch ON m.channel_id=ch.id "
        "JOIN social_servers s ON ch.server_id=s.id "
        "JOIN social_server_members sm ON sm.server_id=s.id AND sm.profile_id=? "
        "JOIN social_users u ON m.sender_id=u.profile_id "
        "WHERE m.content LIKE ? ORDER BY m.created_at DESC LIMIT ?", (profile_id, like, limit)
    ).fetchall()
    for r in srv_rows:
        server_messages.append(_row_to_dict(r))
    return JSONResponse({"conversations": conversations, "messages": messages, "server_messages": server_messages})
@router.post("/conversations")
def create_conversation(data: ConversationCreate, request: Request):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"conv:{profile_id}", 10, 60):
        raise HTTPException(429, "Слишком много диалогов.")
    all_members = list(set(data.member_ids + [profile_id]))
    if len(all_members) < 2:
        raise HTTPException(400, "Нужно минимум 2 участника")
    conv_id = str(uuid.uuid4())
    now = _now()
    is_group = len(all_members) > 2
    conn = _get_db()
    # Check existing private conversation
    if len(all_members) == 2 and not data.title:
        existing = conn.execute(
            "SELECT c.id FROM social_conversations c "
            "JOIN social_conversation_members cm1 ON c.id = cm1.conversation_id AND cm1.profile_id=? "
            "JOIN social_conversation_members cm2 ON c.id = cm2.conversation_id AND cm2.profile_id=? "
            "WHERE c.is_group=0 "
            "AND (SELECT COUNT(*) FROM social_conversation_members WHERE conversation_id=c.id)=2",
            (all_members[0], all_members[1])
        ).fetchone()
        if existing:
            return JSONResponse({"id": existing["id"], "ok": True})
    conn.execute(
        "INSERT INTO social_conversations (id, title, created_at, is_group) VALUES (?,?,?,?)",
        (conv_id, data.title, now, 1 if is_group else 0)
    )
    for mid in all_members:
        _ensure_user(mid)
        conn.execute(
            "INSERT INTO social_conversation_members (conversation_id, profile_id, joined_at) VALUES (?,?,?)",
            (conv_id, mid, now)
        )
    conn.commit()
    return JSONResponse({"id": conv_id, "ok": True})

@router.get("/conversations/{conv_id}/messages")
def get_messages(conv_id: str, request: Request, before: Optional[float] = Query(None), limit: int = Query(50)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    is_member = conn.execute(
        "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
        (conv_id, profile_id)
    ).fetchone()
    if not is_member:
        raise HTTPException(403, "Нет доступа")
    now = _now()
    conn.execute(
        "DELETE FROM social_messages WHERE ttl>0 AND created_at+ttl < ? AND conversation_id=?",
        (now, conv_id)
    )
    conn.commit()
    expired = "(ttl IS NULL OR ttl=0 OR created_at+ttl > ?)"
    if before:
        rows = conn.execute(
            f"SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_bot, "
            "(SELECT 1 FROM social_pinned_messages spm WHERE spm.message_id = m.id AND spm.profile_id=?) as is_pinned "
            "FROM social_messages m "
            "JOIN social_users u ON m.sender_id = u.profile_id "
            f"WHERE m.conversation_id=? AND m.created_at < ? AND {expired} ORDER BY m.created_at DESC LIMIT ?",
            (profile_id, conv_id, before, now, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_bot, "
            "(SELECT 1 FROM social_pinned_messages sm WHERE sm.message_id = m.id AND sm.profile_id=?) as is_pinned "
            "FROM social_messages m "
            "JOIN social_users u ON m.sender_id = u.profile_id "
            f"WHERE m.conversation_id=? AND {expired} ORDER BY m.created_at DESC LIMIT ?",
            (profile_id, conv_id, now, limit)
        ).fetchall()
    # Mark as read
    conn.execute(
        "UPDATE social_conversation_members SET last_read_at=? WHERE conversation_id=? AND profile_id=?",
        (_now(), conv_id, profile_id)
    )
    conn.commit()
    messages = [_row_to_dict(r) for r in rows]
    messages.reverse()
    return JSONResponse(messages)

@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, data: MessageCreate, request: Request):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"msg:{profile_id}", 30, 60):
        raise HTTPException(429, "Слишком много сообщений.")
    conn = _get_db()
    is_member = conn.execute(
        "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
        (conv_id, profile_id)
    ).fetchone()
    if not is_member:
        raise HTTPException(403, "Нет доступа")
    conv = conn.execute("SELECT is_group FROM social_conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        raise HTTPException(404, "Диалог не найден")
    # Ограничение длины: в серверных сообщениях лимит 4000, а здесь он
    # отсутствовал — один клиент мог залить в чат мегабайты текста.
    content = (data.content or "").strip()
    if len(content) > 4000:
        raise HTTPException(400, "Сообщение слишком длинное (максимум 4000 символов)")
    attachments = _safe_attachments(data.attachments)
    # Нельзя отправить ни текст, ни вложение — иначе в переписке появляются
    # пустые «пузыри» (так приходило от клиента с потерянным вводом).
    if not content and attachments == "[]":
        raise HTTPException(400, "Пустое сообщение")
    if not conv["is_group"]:
        other = conn.execute(
            "SELECT profile_id FROM social_conversation_members WHERE conversation_id=? AND profile_id!=? LIMIT 1",
            (conv_id, profile_id)
        ).fetchone()
        if other:
            blocked = conn.execute(
                "SELECT 1 FROM social_friendships WHERE status='blocked' AND "
                "((requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?))",
                (profile_id, other["profile_id"], other["profile_id"], profile_id)
            ).fetchone()
            if blocked:
                raise HTTPException(403, "Невозможно отправить сообщение: пользователь заблокирован")
    msg_id = str(uuid.uuid4())
    now = _now()
    ttl = max(0, min(float(data.ttl or 0), 86400))
    conn.execute(
        "INSERT INTO social_messages (id, conversation_id, sender_id, content, created_at, attachments, reply_to_id, ttl) VALUES (?,?,?,?,?,?,?,?)",
        (msg_id, conv_id, profile_id, content, now, attachments, data.reply_to_id, ttl)
    )
    conn.execute("UPDATE social_conversations SET updated_at=? WHERE id=?", (now, conv_id))
    # Notify other members
    members = conn.execute(
        "SELECT profile_id, is_muted FROM social_conversation_members WHERE conversation_id=? AND profile_id!=?",
        (conv_id, profile_id)
    ).fetchall()
    for m in members:
        if m["is_muted"]:
            continue
        _create_notification(conn, m["profile_id"], "message", profile_id, "conversation", conv_id)
    conn.commit()
    # Push to connected WebSocket clients
    try:
        for m in members:
            mid = m["profile_id"]
            if mid in manager.all_connections:
                await manager.send_to_profile(mid, {
                    "type": "new_message",
                    "conversation_id": conv_id,
                    "message_id": msg_id
                })
    except Exception:
        swallow.note('social_module.send_message:L2443')
        pass
    # Боты: ответ генерируется в фоне, чтобы не держать HTTP-ответ
    # пользователя на генерации LLM (восьмисекундный запрос @bot «привет»).
    _notify_bots("group" if conv["is_group"] else "dm", conv_id, profile_id, content,
                 msg_id)
    _notify_ai("group" if conv["is_group"] else "dm", conv_id, profile_id, content,
               msg_id)
    return JSONResponse({"id": msg_id, "ok": True})

def _notify_bots(chat_kind: str, chat_id: str, sender_id: str, text: str,
                  msg_id: str = ""):
    """Передать новое сообщение ботам, привязанным к чату.

    Обёртка над social_bot_engine.dispatch: движок лежит в отдельном файле,
    а здесь его не должно быть видно при первом же сбое импорта. Ответ
    бота уходит фоновой задачей — отправитель не ждёт генерацию.
    """
    if not (text or "").strip():
        return
    if msg_id:
        # Queue for external bots: they receive messages through
        # getUpdates, i.e. also via a queue rather than directly. A
        # failure here must never break sending of a human message.
        try:
            import social_bot_updates
            social_bot_updates.enqueue(chat_kind, chat_id, sender_id, text, msg_id)
        except Exception:
            swallow.note('social_module._notify_bots:L2471')
            pass
    try:
        import social_bot_engine
        social_bot_engine.dispatch(chat_kind, chat_id, sender_id, text)
    except Exception:
        swallow.note('social_module._notify_bots:L2477')
        pass


def _notify_ai(chat_kind: str, chat_id: str, sender_id: str, text: str,
               msg_id: str = "", channel_id: str = ""):
    """Автоответ AI из настроек чата. Отдельно от ботов: бот отвечает
    своим именем, а автоответ — именем самого пользователя, если он так
    выбрал. Падение AI не должно ломать отправку обычного сообщения."""
    try:
        import social_ai_module
        social_ai_module.notify(chat_kind, chat_id, sender_id, text, msg_id,
                                channel_id)
    except Exception:
        swallow.note('social_module._notify_ai:L2491')
        pass

@router.put("/messages/{msg_id}/read")
async def mark_read(msg_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute(
        "SELECT sender_id, conversation_id FROM social_messages WHERE id=? AND sender_id!=?",
        (msg_id, profile_id)
    ).fetchone()
    conn.execute("UPDATE social_messages SET is_read=1 WHERE id=? AND sender_id!=?", (msg_id, profile_id))
    conn.commit()
    if row:
        try:
            sender = row["sender_id"]
            if sender in manager.all_connections:
                await manager.send_to_profile(sender, {
                    "type": "message_read",
                    "message_id": msg_id,
                    "conversation_id": row["conversation_id"],
                    "profile_id": profile_id
                })
        except Exception:
            swallow.note('social_module.mark_read:L2515')
            pass
    return JSONResponse({"ok": True})

def _member_exists(conn, conv_id: str, profile_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
        (conv_id, profile_id)
    ).fetchone() is not None

@router.put("/conversations/{conv_id}/pin")
def toggle_conversation_pin(conv_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    me = conn.execute(
        "SELECT is_pinned FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
        (conv_id, profile_id)
    ).fetchone()
    new = 0 if me["is_pinned"] else 1
    conn.execute(
        "UPDATE social_conversation_members SET is_pinned=? WHERE conversation_id=? AND profile_id=?",
        (new, conv_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True, "is_pinned": new})

@router.put("/conversations/{conv_id}/mute")
def toggle_conversation_mute(conv_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    me = conn.execute(
        "SELECT is_muted FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
        (conv_id, profile_id)
    ).fetchone()
    new = 0 if me["is_muted"] else 1
    conn.execute(
        "UPDATE social_conversation_members SET is_muted=? WHERE conversation_id=? AND profile_id=?",
        (new, conv_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True, "is_muted": new})

@router.put("/conversations/{conv_id}/folder")
def move_conversation_to_folder(conv_id: str, data: ChatFolderMove, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    conn.execute(
        "UPDATE social_conversation_members SET folder_id=? WHERE conversation_id=? AND profile_id=?",
        (data.folder_id, conv_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.put("/conversations/{conv_id}/archive")
def set_conversation_archived(conv_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    archived = 1 if data.get("archived", False) else 0
    conn.execute(
        "UPDATE social_conversation_members SET is_archived=? WHERE conversation_id=? AND profile_id=?",
        (archived, conv_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True, "is_archived": archived})

@router.post("/conversations/{conv_id}/leave")
async def leave_conversation(conv_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(404, "Диалог не найден")
    conn.execute("DELETE FROM social_conversation_members WHERE conversation_id=? AND profile_id=?", (conv_id, profile_id))
    conn.execute("UPDATE social_conversation_members SET last_read_at=0 WHERE conversation_id=?", (conv_id,))
    conn.execute(
        "INSERT INTO social_messages (id, conversation_id, sender_id, content, created_at, system) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), conv_id, profile_id, "покинул(а) чат", _now(), "leave")
    )
    conn.commit()
    # Update members in realtime
    try:
        members = conn.execute(
            "SELECT profile_id FROM social_conversation_members WHERE conversation_id=?", (conv_id,)).fetchall()
        for m in members:
            if m["profile_id"] in manager.all_connections:
                await manager.send_to_profile(m["profile_id"], {
                    "type": "member_left", "conversation_id": conv_id, "profile_id": profile_id
                })
    except Exception:
        swallow.note('social_module.leave_conversation:L2611')
        pass
    return JSONResponse({"ok": True})

@router.get("/conversations/{conv_id}/export")
def export_conversation(conv_id: str, request: Request):
    """Экспорт истории диалога в JSON (только для участника)."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    conv = conn.execute("SELECT * FROM social_conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        raise HTTPException(404, "Диалог не найден")
    members = conn.execute(
        "SELECT cm.profile_id, u.display_name FROM social_conversation_members cm "
        "JOIN social_users u ON cm.profile_id=u.profile_id WHERE cm.conversation_id=?", (conv_id,)
    ).fetchall()
    msgs = conn.execute(
        "SELECT m.*, u.display_name FROM social_messages m "
        "JOIN social_users u ON m.sender_id=u.profile_id WHERE m.conversation_id=? ORDER BY m.created_at ASC", (conv_id,)
    ).fetchall()
    export = {
        "format": "infinity-social-conversation",
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "conversation": {
            "id": conv["id"],
            "title": conv["title"] or "",
            "is_group": bool(conv["is_group"]),
            "created_at": conv["created_at"],
        },
        "members": [_row_to_dict(m) for m in members],
        "messages": [_row_to_dict(m) for m in msgs],
    }
    fname = f"conversation_{conv_id[:8]}.json"
    return Response(
        content=json.dumps(export, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"}
    )

@router.delete("/conversations/{conv_id}/all")
async def delete_conversation_all(conv_id: str, request: Request):
    """Удалить диалог и историю у всех участников."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    members = conn.execute(
        "SELECT profile_id FROM social_conversation_members WHERE conversation_id=?", (conv_id,)
    ).fetchall()
    conn.execute("DELETE FROM social_pinned_messages WHERE message_id IN (SELECT id FROM social_messages WHERE conversation_id=?)", (conv_id,))
    conn.execute("DELETE FROM social_messages WHERE conversation_id=?", (conv_id,))
    conn.execute("DELETE FROM social_conversation_members WHERE conversation_id=?", (conv_id,))
    conn.execute("DELETE FROM social_conversations WHERE id=?", (conv_id,))
    conn.commit()
    try:
        for m in members:
            if m["profile_id"] in manager.all_connections:
                await manager.send_to_profile(m["profile_id"], {
                    "type": "conversation_deleted", "conversation_id": conv_id
                })
    except Exception:
        swallow.note('social_module.delete_conversation_all:L2675')
        pass
    return JSONResponse({"ok": True})

@router.put("/conversations/{conv_id}/members")
async def update_conversation_members(conv_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    conv = conn.execute("SELECT * FROM social_conversations WHERE id=?", (conv_id,)).fetchone()
    if not conv:
        raise HTTPException(404, "Диалог не найден")
    now = _now()
    adding = data.get("add", []) or []
    removing = data.get("remove", []) or []
    # Личный диалог на двоих нельзя превращать в групповой этим запросом и
    # нельзя «выкинуть» из него собеседника: для этого создаётся новый
    # групповой чат. Раньше добавление третьего молча переписывало is_group.
    if not conv["is_group"] and (adding or removing):
        raise HTTPException(400, "Личный диалог нельзя изменить — создайте групповой чат")
    added, removed = [], []
    for mid in adding:
        if mid == profile_id:
            continue
        _ensure_user(mid)
        is_member = conn.execute(
            "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
            (conv_id, mid)
        ).fetchone()
        if is_member:
            continue
        conn.execute(
            "INSERT INTO social_conversation_members (conversation_id, profile_id, joined_at) VALUES (?,?,?)",
            (conv_id, mid, now)
        )
        added.append(mid)
    for mid in removing:
        if mid == profile_id:
            continue
        conn.execute(
            "DELETE FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
            (conv_id, mid)
        )
        removed.append(mid)
    if added:
        if not conv["is_group"]:
            conn.execute("UPDATE social_conversations SET is_group=1 WHERE id=?", (conv_id,))
        if not (conv["title"] or "").strip():
            conn.execute(
                "UPDATE social_conversations SET title=? WHERE id=?",
                (f"Групповой чат {now:.0f}", conv_id)
            )
        for mid in added:
            conn.execute(
                "INSERT INTO social_messages (id, conversation_id, sender_id, content, created_at, system) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), conv_id, profile_id, "присоединился(ась) к чату", now, "join")
            )
    for mid in removed:
        conn.execute(
            "INSERT INTO social_messages (id, conversation_id, sender_id, content, created_at, system) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), conv_id, profile_id, "покинул(а) чат", now, "leave")
        )
    conn.commit()
    for mid in added:
        _create_notification(conn, mid, "group_add", profile_id, "conversation", conv_id)
        try:
            if mid in manager.all_connections:
                await manager.send_to_profile(mid, {
                    "type": "member_added", "conversation_id": conv_id
                })
        except Exception:
            swallow.note('social_module.update_conversation_members:L2747')
            pass
    members = conn.execute(
        "SELECT cm.*, u.display_name, u.avatar_path, u.avatar_crop, u.last_seen "
        "FROM social_conversation_members cm JOIN social_users u ON cm.profile_id = u.profile_id "
        "WHERE cm.conversation_id = ?", (conv_id,)
    ).fetchall()
    return JSONResponse({"ok": True, "added": added, "removed": removed,
                         "members": [_row_to_dict(m) for m in members]})

@router.post("/calls/invite")
async def call_invite(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conv_id = data.get("conversation_id", "")
    media = data.get("media", "audio")
    if media not in ("audio", "video"):
        media = "audio"
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    members = conn.execute(
        "SELECT profile_id FROM social_conversation_members WHERE conversation_id=? AND profile_id!=?",
        (conv_id, profile_id)
    ).fetchall()
    payload = json.dumps({"type": "call_invite", "media": media, "conversation_id": conv_id, "caller": profile_id}, ensure_ascii=False)
    for m in members:
        conn.execute(
            "INSERT INTO social_p2p_signals (id, from_id, to_id, conversation_id, signal, created_at) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), profile_id, m["profile_id"], conv_id, payload, _now())
        )
        try:
            if m["profile_id"] in manager.all_connections:
                await manager.send_to_profile(m["profile_id"], {
                    "type": "call_invite", "conversation_id": conv_id, "media": media, "from": profile_id
                })
        except Exception:
            swallow.note('social_module.call_invite:L2783')
            pass
    conn.commit()
    return JSONResponse({"ok": True, "members": [m["profile_id"] for m in members]})

@router.post("/calls/respond")
async def call_respond(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conv_id = data.get("conversation_id", "")
    accept = bool(data.get("accept", False))
    conn = _get_db()
    row = conn.execute(
        "SELECT from_id FROM social_p2p_signals WHERE to_id=? AND conversation_id=? AND is_consumed=1 "
        "AND json_extract(signal,'$.type')='call_invite' ORDER BY created_at DESC LIMIT 1",
        (profile_id, conv_id)
    ).fetchone()
    caller = (row["from_id"] if row else None) or data.get("caller", "")
    if not caller:
        raise HTTPException(404, "Не найден инициатор звонка")
    payload = json.dumps({"type": "call_accept" if accept else "call_decline", "media": data.get("media", "audio"),
                          "conversation_id": conv_id, "from": profile_id}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO social_p2p_signals (id, from_id, to_id, conversation_id, signal, created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), profile_id, caller, conv_id, payload, _now())
    )
    conn.commit()
    try:
        if caller in manager.all_connections:
            await manager.send_to_profile(caller, {
                "type": "call_respond", "conversation_id": conv_id, "accept": accept, "from": profile_id
            })
    except Exception:
        swallow.note('social_module.call_respond:L2815')
        pass
    return JSONResponse({"ok": True})

@router.get("/chat-folders")
def get_chat_folders(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM social_chat_folders WHERE owner_id=? ORDER BY created_at", (profile_id,)
    ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

@router.post("/chat-folders")
def create_chat_folder(data: ChatFolderCreate, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    folder_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_chat_folders (id, owner_id, name, created_at) VALUES (?,?,?,?)",
        (folder_id, profile_id, data.name.strip() or "Новая папка", _now())
    )
    conn.commit()
    return JSONResponse({"id": folder_id, "ok": True})

@router.delete("/chat-folders/{folder_id}")
def delete_chat_folder(folder_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute("DELETE FROM social_chat_folders WHERE id=? AND owner_id=?", (folder_id, profile_id))
    conn.execute(
        "UPDATE social_conversation_members SET folder_id='' WHERE folder_id=? AND profile_id=?",
        (folder_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/conversations/{conv_id}/attachments")
def get_conversation_attachments(conv_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    rows = conn.execute(
        "SELECT id, sender_id, attachments, created_at FROM social_messages "
        "WHERE conversation_id=? AND attachments != '[]' AND attachments != '' "
        "ORDER BY created_at DESC", (conv_id,)
    ).fetchall()
    attachments = []
    for r in rows:
        try:
            items = json.loads(r["attachments"])
        except Exception:
            swallow.note('social_module.get_conversation_attachments:L2868')
            continue
        for it in items:
            if isinstance(it, dict):
                path = it.get("path") or it.get("src")
            else:
                path = it
            if not path:
                continue
            att_type = it.get("type") if isinstance(it, dict) else _guess_media_type(str(path))
            attachments.append({
                "message_id": r["id"],
                "sender_id": r["sender_id"],
                "created_at": r["created_at"],
                "path": path,
                "type": att_type,
                "name": it.get("name") if isinstance(it, dict) else "",
                "size": it.get("size", 0) if isinstance(it, dict) else 0
            })
    return JSONResponse(attachments)

@router.put("/messages/{msg_id}")
def edit_message(msg_id: str, data: MessageEditBody, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT sender_id, conversation_id FROM social_messages WHERE id=?", (msg_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    if row["sender_id"] != profile_id:
        raise HTTPException(403, "Нельзя редактировать чужое сообщение")
    if not _member_exists(conn, row["conversation_id"], profile_id):
        raise HTTPException(403, "Нет доступа")
    conn.execute(
        "UPDATE social_messages SET content=?, edited_at=? WHERE id=?",
        (data.content, _now(), msg_id)
    )
    conn.commit()
    return JSONResponse({"ok": True, "id": msg_id})

@router.delete("/messages/{msg_id}")
def delete_message(msg_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT sender_id, conversation_id FROM social_messages WHERE id=?", (msg_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    if row["sender_id"] != profile_id:
        raise HTTPException(403, "Нельзя удалить чужое сообщение")
    if not _member_exists(conn, row["conversation_id"], profile_id):
        raise HTTPException(403, "Нет доступа")
    conn.execute("DELETE FROM social_messages WHERE id=?", (msg_id,))
    conn.execute("DELETE FROM social_pinned_messages WHERE message_id=?", (msg_id,))
    conn.commit()
    return JSONResponse({"ok": True, "id": msg_id})

@router.put("/messages/{msg_id}/pin")
def toggle_message_pin(msg_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute("SELECT id, conversation_id FROM social_messages WHERE id=?", (msg_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Сообщение не найдено")
    if not _member_exists(conn, row["conversation_id"], profile_id):
        raise HTTPException(403, "Нет доступа")
    existing = conn.execute(
        "SELECT 1 FROM social_pinned_messages WHERE message_id=? AND profile_id=?",
        (msg_id, profile_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM social_pinned_messages WHERE message_id=? AND profile_id=?", (msg_id, profile_id))
        pinned = False
    else:
        conn.execute(
            "INSERT INTO social_pinned_messages (message_id, profile_id, pinned_at) VALUES (?,?,?)",
            (msg_id, profile_id, _now())
        )
        pinned = True
    conn.commit()
    return JSONResponse({"ok": True, "pinned": pinned})

@router.get("/conversations/{conv_id}/messages/search")
def search_conversation_messages(request: Request, conv_id: str, q: str = Query(""), limit: int = Query(50)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _member_exists(conn, conv_id, profile_id):
        raise HTTPException(403, "Нет доступа")
    pattern = f"%{q}%"
    rows = conn.execute(
        "SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, "
        "(SELECT 1 FROM social_pinned_messages sm WHERE sm.message_id = m.id AND sm.profile_id=?) as is_pinned "
        "FROM social_messages m "
        "JOIN social_users u ON m.sender_id = u.profile_id "
        "WHERE m.conversation_id=? AND m.content LIKE ? "
        "AND (m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?) "
        "ORDER BY m.created_at DESC LIMIT ?",
        (profile_id, conv_id, pattern, _now(), limit)
    ).fetchall()
    messages = [_row_to_dict(r) for r in rows]
    messages.reverse()
    return JSONResponse(messages)

# --- Feed API ---

@router.get("/feed")
def get_feed(request: Request, offset: int = Query(0), limit: int = Query(30)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT p.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_online, u.ap_actor_url, u.is_federated "
        "FROM social_feeds f JOIN social_posts p ON f.post_id = p.id "
        "JOIN social_users u ON p.author_id = u.profile_id "
        "WHERE f.user_id = ? ORDER BY f.created_at DESC LIMIT ? OFFSET ?",
        (profile_id, limit, offset)
    ).fetchall()
    posts = []
    for r in rows:
        post = _row_to_dict(r)
        if post.get("ap_actor_url"):
            post["is_remote"] = True
            post["actor_url"] = post.pop("ap_actor_url")
        else:
            post["is_remote"] = False
            post["actor_url"] = ""
        if post.get("repost_id"):
            orig = conn.execute(
                "SELECT p.content, u.display_name, u.profile_id FROM social_posts p "
                "JOIN social_users u ON p.author_id = u.profile_id WHERE p.id=?",
                (post["repost_id"],)
            ).fetchone()
            if orig:
                post["repost_author_name"] = orig["display_name"] or orig["profile_id"]
                post["repost_content"] = orig["content"]
        liked = conn.execute(
            "SELECT 1 FROM social_likes WHERE user_id=? AND target_type='post' AND target_id=?",
            (profile_id, post["id"])
        ).fetchone()
        post["liked_by_me"] = liked is not None
        posts.append(post)
    return JSONResponse(posts)

# --- Notifications API ---

def _create_notification(conn, user_id: str, notif_type: str, actor_id: str, target_type: str = "", target_id: str = ""):
    _ensure_user(user_id, _get_profile_name(user_id))
    try:
        conn.execute(
            "INSERT INTO social_notifications (id, user_id, type, actor_id, target_type, target_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id, notif_type, actor_id, target_type, target_id, _now())
        )
    except Exception as e:
        logger.warning(f"Notification error: {e}")

@router.get("/notifications")
def get_notifications(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT n.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_notifications n "
        "JOIN social_users u ON n.actor_id = u.profile_id "
        "WHERE n.user_id = ? ORDER BY n.created_at DESC LIMIT 50", (profile_id,)
    ).fetchall()
    unread = conn.execute(
        "SELECT COUNT(*) FROM social_notifications WHERE user_id=? AND is_read=0", (profile_id,)
    ).fetchone()[0]
    return JSONResponse({"notifications": [_row_to_dict(r) for r in rows], "unread_count": unread})

@router.put("/notifications/{notif_id}/read")
def mark_notification_read(notif_id: str, request: Request):
    # Раньше обработчик вообще не принимал request и не сверял владельца:
    # зная идентификатор, можно было отметить чужие уведомления
    # прочитанными, а по GET они не показывались (фильтр по user_id).
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute(
        "UPDATE social_notifications SET is_read=1 WHERE id=? AND user_id=?",
        (notif_id, profile_id))
    conn.commit()
    if not row.rowcount:
        raise HTTPException(404, "Уведомление не найдено")
    return JSONResponse({"ok": True})

@router.put("/notifications/read-all")
def mark_all_read(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute("UPDATE social_notifications SET is_read=1 WHERE user_id=?", (profile_id,))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Social inbox (unread messages grouped by conversation, without marking as read) ---

@router.get("/inbox")
def get_social_inbox(request: Request):
    """Непрочитанные сообщения текущего пользователя, сгруппированные по диалогу.
    Не помечает сообщения прочитанными (это делает /conversations/{id}/read)."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT c.id, c.title, c.is_group, cm.last_read_at "
        "FROM social_conversations c "
        "JOIN social_conversation_members cm ON c.id = cm.conversation_id "
        "WHERE cm.profile_id = ?", (profile_id,)
    ).fetchall()
    items = []
    total = 0
    for r in rows:
        conv = _row_to_dict(r)
        last_read = conv.get("last_read_at", 0) or 0
        unread = conn.execute(
            "SELECT COUNT(*) FROM social_messages "
            "WHERE conversation_id=? AND sender_id!=? AND created_at > ? "
            "AND (ttl IS NULL OR ttl=0 OR created_at+ttl > ?)",
            (conv["id"], profile_id, last_read, _now())
        ).fetchone()[0]
        if unread <= 0:
            continue
        total += unread
        members = conn.execute(
            "SELECT u.display_name, u.avatar_path, u.avatar_crop "
            "FROM social_conversation_members cm "
            "JOIN social_users u ON cm.profile_id = u.profile_id "
            "WHERE cm.conversation_id=? AND cm.profile_id!=?",
            (conv["id"], profile_id)
        ).fetchall()
        is_group = bool(conv.get("is_group", 0))
        if is_group:
            title = conv.get("title") or "Группа"
            avatar = members[0]["avatar_path"] if members else ""
        else:
            other = members[0] if members else None
            title = (other["display_name"] if other else None) or "Собеседник"
            avatar = (other["avatar_path"] if other else None) or ""
        msg_rows = conn.execute(
            "SELECT m.id, m.content, m.created_at, u.display_name, u.avatar_path "
            "FROM social_messages m JOIN social_users u ON m.sender_id = u.profile_id "
            "WHERE m.conversation_id=? AND m.sender_id!=? AND m.created_at > ? "
            "AND (m.ttl IS NULL OR m.ttl=0 OR m.created_at+m.ttl > ?) "
            "ORDER BY m.created_at ASC LIMIT 10",
            (conv["id"], profile_id, last_read, _now())
        ).fetchall()
        messages = []
        last_at = last_read
        for m in msg_rows:
            md = _row_to_dict(m)
            ts = md.get("created_at", 0) or 0
            last_at = max(last_at, ts)
            messages.append({
                "id": md["id"],
                "sender_name": md.get("display_name", "") or "",
                "sender_avatar": md.get("avatar_path", "") or "",
                "content": md.get("content", "") or "",
                "created_at": ts,
            })
        items.append({
            "conversation_id": conv["id"],
            "is_group": 1 if is_group else 0,
            "title": title,
            "avatar": avatar or "",
            "unread_count": unread,
            "last_at": last_at,
            "messages": messages,
        })
    items.sort(key=lambda x: x.get("last_at", 0), reverse=True)
    return JSONResponse({"items": items, "total_unread": total})

@router.post("/conversations/{conv_id}/read")
def mark_conversation_read(conv_id: str, request: Request):
    """Пометить диалог прочитанным для текущего пользователя (без загрузки переписки)."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute(
        "UPDATE social_conversation_members SET last_read_at=? WHERE conversation_id=? AND profile_id=?",
        (_now(), conv_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/inbox/read-all")
def mark_inbox_read_all(request: Request):
    """Пометить все диалоги прочитанными."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute(
        "UPDATE social_conversation_members SET last_read_at=? WHERE profile_id=?",
        (_now(), profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

# --- Search API ---

@router.get("/search")
def search_users(request: Request, q: str = Query(""), offset: int = Query(0), limit: int = Query(20)):
    profile_id = _get_active_profile(request)
    # Поиск без ограничения частоты позволял перебор всей базы профилей.
    if not _check_rate_limit(f"usersearch:{profile_id}", 30, 60):
        raise HTTPException(429, "Слишком много запросов поиска. Подождите минуту.")
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    q = (q or "").strip()[:100]
    conn = _get_db()
    if not q:
        rows = conn.execute(
            "SELECT * FROM social_users ORDER BY last_seen DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    else:
        # % и _ в LIKE — это wildcards: без экранирования запрос «а%б»
        # находил совсем не то, а пользователь мог перебрать базу.
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.execute(
            "SELECT * FROM social_users WHERE display_name LIKE ? ESCAPE '\\' OR profile_id LIKE ? ESCAPE '\\' "
            "OR bio LIKE ? ESCAPE '\\' "
            "ORDER BY last_seen DESC LIMIT ? OFFSET ?", (pattern, pattern, pattern, limit, offset)
        ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

# --- Online status ---

@router.post("/online")
def set_online(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    _ensure_user(profile_id)
    conn.execute("UPDATE social_users SET last_seen=?, is_online=1 WHERE profile_id=?", (_now(), profile_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/offline")
def set_offline(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute("UPDATE social_users SET is_online=0 WHERE profile_id=?", (profile_id,))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Media Search (интеграция с media_module) ---

@router.get("/media/search")
def search_media(q: str = Query(""), type: str = Query("all"), limit: int = Query(20)):
    results = {"tracks": [], "videos": []}
    query = q.lower().strip()
    if type in ("all", "music"):
        try:
            from media_module import search_tracks, _get_all_music_tracks
            all_tracks = _get_all_music_tracks()
            if query:
                tracks = [t for t in all_tracks if query in t.get("title","").lower() or query in t.get("artist","").lower() or query in t.get("album","").lower()]
            else:
                tracks = all_tracks
            results["tracks"] = tracks[:limit]
        except Exception as e:
            logger.warning(f"Media search music error: {e}")
    if type in ("all", "video"):
        try:
            from media_module import _get_all_video_files
            all_videos = _get_all_video_files()
            if query:
                videos = [v for v in all_videos if query in v.get("title","").lower() or query in v.get("filename","").lower() or query in v.get("overview","").lower()]
            else:
                videos = all_videos
            results["videos"] = videos[:limit]
        except Exception as e:
            logger.warning(f"Media search video error: {e}")
    return JSONResponse(results)

# --- Extended Profile Fields API ---

@router.get("/profile/fields/list")
def get_profile_field_definitions():
    fields = [
        {"key": "phone", "label": "Телефон", "icon": "fa-phone", "type": "tel", "section": "contact"},
        {"key": "email", "label": "Email", "icon": "fa-envelope", "type": "email", "section": "contact"},
        {"key": "gender", "label": "Пол", "icon": "fa-venus-mars", "type": "select", "options": ["Мужской","Женский","Другой"], "section": "personal"},
        {"key": "relationship", "label": "Семейное положение", "icon": "fa-heart", "type": "select", "options": ["Не указано","Холост/Не замужем","В отношениях","Помолвлен/а","Женат/Замужем","В гражданском браке","В разводе","Вдовец/Вдова"], "section": "personal"},
        {"key": "birthday", "label": "День рождения", "icon": "fa-birthday-cake", "type": "date", "section": "personal"},
        {"key": "country", "label": "Страна", "icon": "fa-globe", "type": "text", "section": "location"},
        {"key": "city", "label": "Город", "icon": "fa-city", "type": "text", "section": "location"},
        {"key": "address", "label": "Адрес", "icon": "fa-home", "type": "text", "section": "location"},
        {"key": "education", "label": "Образование", "icon": "fa-graduation-cap", "type": "text", "section": "work"},
        {"key": "work", "label": "Место работы", "icon": "fa-briefcase", "type": "text", "section": "work"},
        {"key": "position", "label": "Должность", "icon": "fa-user-tie", "type": "text", "section": "work"},
        {"key": "skills", "label": "Навыки", "icon": "fa-code", "type": "textarea", "section": "interests"},
        {"key": "languages", "label": "Языки", "icon": "fa-language", "type": "text", "section": "interests"},
        {"key": "interests", "label": "Интересы", "icon": "fa-star", "type": "textarea", "section": "interests"},
        {"key": "favorite_music", "label": "Любимая музыка", "icon": "fa-music", "type": "text", "section": "interests"},
        {"key": "favorite_movies", "label": "Любимые фильмы", "icon": "fa-film", "type": "text", "section": "interests"},
        {"key": "favorite_books", "label": "Любимые книги", "icon": "fa-book", "type": "text", "section": "interests"},
        {"key": "favorite_games", "label": "Любимые игры", "icon": "fa-gamepad", "type": "text", "section": "interests"},
        {"key": "about_me", "label": "О себе", "icon": "fa-quote-right", "type": "textarea", "section": "personal"},
        {"key": "religion", "label": "Религия", "icon": "fa-pray", "type": "text", "section": "personal"},
        {"key": "political", "label": "Политические взгляды", "icon": "fa-landmark", "type": "text", "section": "personal"},
        {"key": "life_position", "label": "Жизненная позиция", "icon": "fa-compass", "type": "text", "section": "personal"},
        {"key": "tg", "label": "Telegram", "icon": "fa-telegram", "type": "text", "section": "social"},
        {"key": "instagram", "label": "Instagram", "icon": "fa-instagram", "type": "text", "section": "social"},
        {"key": "twitter", "label": "Twitter", "icon": "fa-twitter", "type": "text", "section": "social"},
        {"key": "github", "label": "GitHub", "icon": "fa-github", "type": "text", "section": "social"},
        {"key": "youtube", "label": "YouTube", "icon": "fa-youtube", "type": "text", "section": "social"},
        {"key": "tiktok", "label": "TikTok", "icon": "fa-tiktok", "type": "text", "section": "social"},
    ]
    return JSONResponse(fields)

@router.put("/profile/fields")
def update_profile_fields(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    conn = _get_db()
    fields = data.get("fields", {})  # {key: value}
    privacy = data.get("privacy", {})  # {key: 0|1|2}
    for key, value in fields.items():
        p = privacy.get(key, 0)
        conn.execute(
            "INSERT OR REPLACE INTO social_profile_extras (profile_id, field_key, field_value, privacy) VALUES (?,?,?,?)",
            (profile_id, key, str(value), p)
        )
    # Update privacy_settings as JSON
    ps = data.get("privacy_settings", {})
    if ps:
        conn.execute("UPDATE social_users SET privacy_settings=? WHERE profile_id=?", (json.dumps(ps), profile_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/profile/photo")
def upload_profile_photo(request: Request, file: UploadFile = File(...), caption: str = ""):
    profile_id = _get_active_profile(request)
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат")
    filename = f"{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    url = f"/api/social/file/root/{filename}"
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_profile_photos (id, profile_id, url, caption, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), profile_id, url, caption, _now())
    )
    conn.commit()
    return JSONResponse({"ok": True, "url": url})

@router.delete("/profile/photo/{photo_id}")
def delete_profile_photo(photo_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=? AND profile_id=?", (photo_id, profile_id)).fetchone()
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    conn.execute("DELETE FROM social_profile_photos WHERE id=?", (photo_id,))
    conn.commit()
    # Try to delete file
    try:
        fname = os.path.basename(photo["url"])
        os.remove(os.path.join(UPLOADS_DIR, fname))
    except Exception:
        swallow.note('social_module.delete_profile_photo:L3321')
        pass
    return JSONResponse({"ok": True})

# --- Upload API ---

@router.post("/upload/avatar")
def upload_avatar(request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"avatar:{profile_id}", 3, 60):
        raise HTTPException(429, "Слишком много загрузок аватара.")
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат. Разрешены: jpg, png, gif, webp")
    filename = f"{profile_id}{ext}"
    filepath = os.path.join(AVATARS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    conn = _get_db()
    conn.execute("UPDATE social_users SET avatar_path=? WHERE profile_id=?", (f"/api/social/file/avatars/{filename}", profile_id))
    conn.commit()
    return JSONResponse({"ok": True, "path": f"/api/social/file/avatars/{filename}"})

@router.post("/upload/cover")
def upload_cover(request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат. Разрешены: jpg, png, gif, webp")
    filename = f"{profile_id}{ext}"
    filepath = os.path.join(COVERS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    conn = _get_db()
    conn.execute("UPDATE social_users SET cover_path=? WHERE profile_id=?", (f"/api/social/file/covers/{filename}", profile_id))
    conn.commit()
    return JSONResponse({"ok": True, "path": f"/api/social/file/covers/{filename}"})

# --- Chat attachments (files from computer/library) ---

@router.post("/upload/chat-file")
def upload_chat_file(request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    if not _check_rate_limit(f"chatfile:{profile_id}", 60, 60):
        raise HTTPException(429, "Слишком много загрузок.")
    orig_name = file.filename or "file"
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in _ALLOWED_FILE_EXT:
        raise HTTPException(400, f"Недопустимый формат: {ext}")
    filename = f"{profile_id}_{int(time.time())}_{str(uuid.uuid4())[:8]}{ext}"
    filepath = os.path.join(CHAT_MEDIA_DIR, filename)
    _save_upload(file, filepath, _MAX_CHAT_FILE_BYTES)
    return JSONResponse({
        "ok": True,
        "path": f"/api/social/file/chat_media/{filename}",
        "type": _guess_media_type(orig_name),
        "name": orig_name,
        "size": os.path.getsize(filepath),
    })

# --- Multi-avatar gallery (original + crop params, flip-through) ---

@router.get("/avatars")
def list_avatars(request: Request, profile_id: Optional[str] = Query(None)):
    owner = profile_id or _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, path, crop, is_active, created_at FROM social_avatars WHERE profile_id=? ORDER BY is_active DESC, sort_order, created_at",
        (owner,)
    ).fetchall()
    result = []
    for r in rows:
        d = _row_to_dict(r)
        try:
            d["crop"] = json.loads(d.get("crop") or '{"zoom":1,"x":0.5,"y":0.5}')
        except Exception:
            swallow.note('social_module.list_avatars:L3396')
            d["crop"] = {"zoom": 1, "x": 0.5, "y": 0.5}
        result.append(d)
    return JSONResponse(result)

@router.post("/avatars")
def upload_avatar_original(request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    if not _check_rate_limit(f"avatar:{profile_id}", 5, 60):
        raise HTTPException(429, "Слишком много загрузок аватара.")
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат. Разрешены: jpg, png, gif, webp")
    filename = f"{profile_id}_{str(uuid.uuid4())[:8]}{ext}"
    filepath = os.path.join(AVATARS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    url = f"/api/social/file/avatars/{filename}"
    conn = _get_db()
    count = conn.execute("SELECT COUNT(*) FROM social_avatars WHERE profile_id=?", (profile_id,)).fetchone()[0]
    is_active = 1 if count == 0 else 0
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_avatars (id, profile_id, path, is_active, sort_order, created_at) VALUES (?,?,?,?,?,?)",
        (aid, profile_id, url, is_active, count, _now())
    )
    if is_active:
        conn.execute("UPDATE social_users SET avatar_path=? WHERE profile_id=?", (url, profile_id))
    conn.commit()
    return JSONResponse({"ok": True, "id": aid, "path": url, "is_active": bool(is_active)})

@router.put("/avatars/{avatar_id}")
def update_avatar_crop(avatar_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    crop = data.get("crop", {})
    try:
        zoom = max(0.5, min(4, float(crop.get("zoom", 1))))
        x = max(0.0, min(1.0, float(crop.get("x", 0.5))))
        y = max(0.0, min(1.0, float(crop.get("y", 0.5))))
    except (TypeError, ValueError):
        swallow.note('social_module.update_avatar_crop:L3436')
        zoom, x, y = 1.0, 0.5, 0.5
    crop_json = json.dumps({"zoom": zoom, "x": x, "y": y})
    conn = _get_db()
    av = conn.execute("SELECT * FROM social_avatars WHERE id=? AND profile_id=?", (avatar_id, profile_id)).fetchone()
    if not av:
        raise HTTPException(404, "Аватар не найден")
    conn.execute("UPDATE social_avatars SET crop=? WHERE id=?", (crop_json, avatar_id))
    if av["is_active"]:
        conn.execute("UPDATE social_users SET avatar_crop=? WHERE profile_id=?", (crop_json, profile_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/avatars/{avatar_id}/activate")
def activate_avatar(avatar_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    av = conn.execute("SELECT * FROM social_avatars WHERE id=? AND profile_id=?", (avatar_id, profile_id)).fetchone()
    if not av:
        raise HTTPException(404, "Аватар не найден")
    conn.execute("UPDATE social_avatars SET is_active=0 WHERE profile_id=?", (profile_id,))
    conn.execute("UPDATE social_avatars SET is_active=1 WHERE id=?", (avatar_id,))
    conn.execute("UPDATE social_users SET avatar_path=?, avatar_crop=? WHERE profile_id=?", (av["path"], av["crop"], profile_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.delete("/avatars/{avatar_id}")
def delete_avatar(avatar_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    av = conn.execute("SELECT * FROM social_avatars WHERE id=? AND profile_id=?", (avatar_id, profile_id)).fetchone()
    if not av:
        raise HTTPException(404, "Аватар не найден")
    conn.execute("DELETE FROM social_avatars WHERE id=?", (avatar_id,))
    if av["is_active"]:
        next_av = conn.execute(
            "SELECT * FROM social_avatars WHERE profile_id=? ORDER BY sort_order LIMIT 1", (profile_id,)
        ).fetchone()
        if next_av:
            conn.execute("UPDATE social_avatars SET is_active=1 WHERE id=?", (next_av["id"],))
            conn.execute("UPDATE social_users SET avatar_path=?, avatar_crop=? WHERE profile_id=?", (next_av["path"], next_av["crop"], profile_id))
        else:
            conn.execute("UPDATE social_users SET avatar_path='', avatar_crop='{\"zoom\":1,\"x\":0.5,\"y\":0.5}' WHERE profile_id=?", (profile_id,))
    conn.commit()
    try:
        os.remove(os.path.join(AVATARS_DIR, os.path.basename(av["path"])))
    except Exception:
        swallow.note('social_module.delete_avatar:L3483')
        pass
    return JSONResponse({"ok": True})

# --- Stickers API ---

@router.get("/stickers/packs")
def get_sticker_packs(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM social_sticker_packs WHERE is_default=1 OR owner_id=? ORDER BY is_default DESC, created_at",
        (profile_id,)
    ).fetchall()
    result = []
    for r in rows:
        pack = _row_to_dict(r)
        stickers = conn.execute(
            "SELECT id, url, emoji FROM social_stickers WHERE pack_id=? ORDER BY sort_order, created_at", (pack["id"],)
        ).fetchall()
        pack["stickers"] = [_row_to_dict(s) for s in stickers]
        result.append(pack)
    return JSONResponse(result)

@router.post("/stickers/packs")
def create_sticker_pack(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    name = (data.get("name") or "").strip()[:50]
    if not name:
        raise HTTPException(400, "Название набора обязательно")
    pack_id = str(uuid.uuid4())
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_sticker_packs (id, owner_id, name, is_default, created_at) VALUES (?,?,?,0,?)",
        (pack_id, profile_id, name, _now())
    )
    conn.commit()
    return JSONResponse({"ok": True, "id": pack_id})

@router.post("/stickers/packs/{pack_id}/stickers")
def upload_stickers(pack_id: str, request: Request, files: List[UploadFile] = File(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    pack = conn.execute("SELECT * FROM social_sticker_packs WHERE id=?", (pack_id,)).fetchone()
    if not pack:
        raise HTTPException(404, "Набор не найден")
    if not pack["is_default"] and pack["owner_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    count = conn.execute("SELECT COUNT(*) FROM social_stickers WHERE pack_id=?", (pack_id,)).fetchone()[0]
    saved = []
    for file in files:
        ext = os.path.splitext(file.filename or ".png")[1].lower()
        if ext not in _ALLOWED_IMAGE_EXT:
            continue
        filename = f"{pack_id}_{str(uuid.uuid4())[:8]}{ext}"
        filepath = os.path.join(STICKERS_DIR, filename)
        _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
        url = f"/api/social/file/stickers/{filename}"
        conn.execute(
            "INSERT INTO social_stickers (id, pack_id, url, sort_order, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), pack_id, url, count, _now())
        )
        count += 1
        saved.append(url)
    conn.commit()
    return JSONResponse({"ok": True, "stickers": saved})

@router.delete("/stickers/packs/{pack_id}")
def delete_sticker_pack(pack_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    pack = conn.execute("SELECT * FROM social_sticker_packs WHERE id=?", (pack_id,)).fetchone()
    if not pack:
        raise HTTPException(404, "Набор не найден")
    if pack["is_default"] or pack["owner_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    stickers = conn.execute("SELECT url FROM social_stickers WHERE pack_id=?", (pack_id,)).fetchall()
    conn.execute("DELETE FROM social_sticker_packs WHERE id=?", (pack_id,))
    conn.commit()
    for s in stickers:
        try:
            os.remove(os.path.join(STICKERS_DIR, os.path.basename(s["url"])))
        except Exception:
            swallow.note('social_module.delete_sticker_pack:L3567')
            pass
    return JSONResponse({"ok": True})

@router.delete("/stickers/{sticker_id}")
def delete_sticker(sticker_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    row = conn.execute(
        "SELECT s.*, p.owner_id, p.is_default FROM social_stickers s JOIN social_sticker_packs p ON s.pack_id=p.id WHERE s.id=?",
        (sticker_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Стикер не найден")
    if not row["is_default"] and row["owner_id"] != profile_id:
        raise HTTPException(403, "Нет прав")
    conn.execute("DELETE FROM social_stickers WHERE id=?", (sticker_id,))
    conn.commit()
    try:
        os.remove(os.path.join(STICKERS_DIR, os.path.basename(row["url"])))
    except Exception:
        swallow.note('social_module.delete_sticker:L3588')
        pass
    return JSONResponse({"ok": True})

# --- Reactions API ---

@router.post("/posts/{post_id}/react")
def react_to_post(post_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"react:{profile_id}", 30, 60):
        raise HTTPException(429, "Слишком много реакций.")
    reaction_type = data.get("type", "like")
    conn = _get_db()
    existing = conn.execute("SELECT id, type FROM social_reactions WHERE user_id=? AND post_id=?", (profile_id, post_id)).fetchone()
    was_like = existing and existing["type"] == "like"
    will_be_like = reaction_type == "like"
    if existing:
        conn.execute("UPDATE social_reactions SET type=? WHERE id=?", (reaction_type, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO social_reactions (id, user_id, post_id, type, created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), profile_id, post_id, reaction_type, _now())
        )
    # Adjust likes_count based on before/after state
    if was_like and not will_be_like:
        conn.execute("UPDATE social_posts SET likes_count = MAX(0, likes_count - 1) WHERE id=?", (post_id,))
    elif not was_like and will_be_like:
        conn.execute("UPDATE social_posts SET likes_count = likes_count + 1 WHERE id=?", (post_id,))
        post = conn.execute("SELECT author_id FROM social_posts WHERE id=?", (post_id,)).fetchone()
        if post and post["author_id"] != profile_id:
            _create_notification(conn, post["author_id"], "like", profile_id, "post", post_id)
        try:
            import activitypub_module
            activitypub_module.federate_like(profile_id, post_id, like=True)
        except Exception:
            swallow.note('social_module.react_to_post:L3623')
            pass
    elif was_like and not will_be_like:
        try:
            import activitypub_module
            activitypub_module.federate_like(profile_id, post_id, like=False)
        except Exception:
            swallow.note('social_module.react_to_post:L3630')
            pass
    conn.commit()
    return JSONResponse({"ok": True, "type": reaction_type})

# --- Stories API ---

@router.get("/stories")
def get_stories(request: Request, profile_id: Optional[str] = Query(None)):
    active_id = _get_active_profile(request)
    conn = _get_db()
    now = _now()
    conn.execute("DELETE FROM social_stories WHERE expires_at < ?", (now,))
    conn.commit()
    if profile_id:
        rows = conn.execute(
            "SELECT s.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_stories s JOIN social_users u ON s.author_id = u.profile_id WHERE s.author_id=? AND s.expires_at > ? ORDER BY s.created_at DESC",
            (profile_id, now)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_stories s JOIN social_users u ON s.author_id = u.profile_id WHERE s.expires_at > ? ORDER BY s.created_at DESC",
            (now,)
        ).fetchall()
    stories = []
    for r in rows:
        s = _row_to_dict(r)
        s["viewed"] = bool(conn.execute(
            "SELECT 1 FROM social_viewed_stories WHERE story_id=? AND user_id=?", (s["id"], active_id)
        ).fetchone())
        s["view_count"] = conn.execute(
            "SELECT COUNT(*) as cnt FROM social_viewed_stories WHERE story_id=?", (s["id"],)
        ).fetchone()["cnt"]
        stories.append(s)
    return JSONResponse(stories)

@router.post("/stories/{story_id}/view")
def view_story(story_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute(
        "INSERT OR IGNORE INTO social_viewed_stories (story_id, user_id) VALUES (?,?)",
        (story_id, profile_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/stories")
def create_story(request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"story:{profile_id}", 5, 60):
        raise HTTPException(429, "Слишком много историй.")
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    is_video = ext in ('.mp4', '.webm', '.mov')
    if ext not in (_ALLOWED_IMAGE_EXT | _ALLOWED_VIDEO_EXT):
        raise HTTPException(400, "Недопустимый формат: разрешены изображения и видео (mp4, webm, mov)")
    filename = f"{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(STORIES_DIR, filename)
    _save_upload(file, filepath, _MAX_POST_MEDIA_BYTES, image_only=not is_video)
    media_url = f"/api/social/file/stories/{filename}"
    story_id = str(uuid.uuid4())
    now = _now()
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_stories (id, author_id, media_url, media_type, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (story_id, profile_id, media_url, "video" if is_video else "image", now, now + 86400)
    )
    conn.commit()
    return JSONResponse({"ok": True, "id": story_id})

# --- Albums API ---

@router.get("/albums")
def get_albums(request: Request, profile_id: str = Query(""), viewer_id: str = Query("")):
    pid = profile_id or _get_active_profile(request)
    viewer = viewer_id or _get_active_profile(request)
    conn = _get_db()
    if viewer == pid:
        albums = conn.execute(
            "SELECT * FROM social_albums WHERE profile_id=? AND COALESCE(group_id,'')='' AND COALESCE(channel_id,'')='' ORDER BY created_at DESC",
            (pid,)
        ).fetchall()
    else:
        is_friend = conn.execute(
            "SELECT 1 FROM social_friendships WHERE status='accepted' AND "
            "(requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
            (viewer, pid, pid, viewer)
        ).fetchone() is not None
        albums = conn.execute(
            "SELECT * FROM social_albums WHERE profile_id=? AND COALESCE(group_id,'')='' AND COALESCE(channel_id,'')='' AND ("
            "visibility='public' OR (visibility='' OR visibility IS NULL AND privacy=0) OR "
            "(visibility='friends' AND ?=1)) ORDER BY created_at DESC",
            (pid, 1 if is_friend else 0)
        ).fetchall()
    return JSONResponse([_row_to_dict(a) for a in albums])

@router.post("/albums")
def create_album(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    title = data.get("title", "").strip()
    if not title:
        raise HTTPException(400, "Название альбома обязательно")
    album_id = str(uuid.uuid4())
    now = _now()
    conn = _get_db()
    visibility = data.get("visibility", "")
    if visibility not in ("public", "friends", "private"):
        visibility = "friends"
    conn.execute(
        "INSERT INTO social_albums (id, profile_id, title, description, cover_path, created_at, privacy, visibility) VALUES (?,?,?,?,?,?,?,?)",
        (album_id, profile_id, title, data.get("description", ""), data.get("cover_path", ""), now, data.get("privacy", 0), visibility)
    )
    conn.commit()
    return JSONResponse({"id": album_id, "ok": True})

@router.put("/albums/{album_id}")
def update_album(album_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    album = conn.execute("SELECT * FROM social_albums WHERE id=? AND profile_id=?", (album_id, profile_id)).fetchone()
    if not album:
        raise HTTPException(404, "Альбом не найден")
    for field in ("title", "description", "cover_path", "privacy", "visibility"):
        if field in data:
            conn.execute(f"UPDATE social_albums SET {field}=? WHERE id=?", (data[field], album_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.delete("/albums/{album_id}")
def delete_album(album_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    album = conn.execute("SELECT * FROM social_albums WHERE id=? AND profile_id=?", (album_id, profile_id)).fetchone()
    if not album:
        raise HTTPException(404, "Альбом не найден")
    conn.execute("DELETE FROM social_album_photos WHERE album_id=?", (album_id,))
    conn.execute("DELETE FROM social_albums WHERE id=?", (album_id,))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/albums/{album_id}/photos")
def add_album_photo(album_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    photo_id = data.get("photo_id", "")
    if not photo_id:
        raise HTTPException(400, "photo_id обязателен")
    conn = _get_db()
    album = conn.execute("SELECT * FROM social_albums WHERE id=? AND profile_id=?", (album_id, profile_id)).fetchone()
    if not album:
        raise HTTPException(404, "Альбом не найден")
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=? AND profile_id=?", (photo_id, profile_id)).fetchone()
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO social_album_photos (album_id, photo_id, sort_order, added_at) VALUES (?,?,?,?)",
        (album_id, photo_id, int(time.time()), now)
    )
    count = conn.execute("SELECT COUNT(*) FROM social_album_photos WHERE album_id=?", (album_id,)).fetchone()[0]
    conn.execute("UPDATE social_albums SET photo_count=? WHERE id=?", (count, album_id))
    if not album["cover_path"]:
        conn.execute("UPDATE social_albums SET cover_path=? WHERE id=?", (photo["url"], album_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/albums/{album_id}/photos")
def get_album_photos(album_id: str, request: Request):
    viewer = _get_active_profile(request)
    conn = _get_db()
    album = conn.execute("SELECT * FROM social_albums WHERE id=?", (album_id,)).fetchone()
    owner = album["profile_id"] if album else None
    if album:
        if viewer != owner:
            is_friend = conn.execute(
                "SELECT 1 FROM social_friendships WHERE status='accepted' AND "
                "(requester_id=? AND addressee_id=?) OR (requester_id=? AND addressee_id=?)",
                (viewer, owner, owner, viewer)
            ).fetchone() is not None
            vis = album["visibility"] or ("public" if album["privacy"] == 0 else "private")
            if vis == "private" or (vis == "friends" and not is_friend):
                raise HTTPException(403, "Нет доступа к альбому")
    photos = conn.execute(
        "SELECT p.*, ap.media_type, "
        "  COALESCE(u.display_name, p.profile_id) AS author_name, "
        "  u.avatar_path AS author_avatar, u.avatar_crop AS author_crop, "
        "  (SELECT COUNT(*) FROM social_photo_likes l WHERE l.photo_id = p.id) AS likes_count, "
        "  EXISTS(SELECT 1 FROM social_photo_likes l2 "
        "         WHERE l2.photo_id = p.id AND l2.profile_id = ?) AS liked_by_me "
        "FROM social_profile_photos p "
        "JOIN social_album_photos ap ON p.id=ap.photo_id "
        "LEFT JOIN social_users u ON u.profile_id = p.profile_id "
        "WHERE ap.album_id=? ORDER BY ap.sort_order",
        (viewer, album_id)
    ).fetchall()
    # Права редактирования и удаления: владелец альбома, владелец фото,
    # либо админ/модератор группы, в альбом которой фото лежит.
    out = []
    for p in photos:
        d = _row_to_dict(p)
        d["likes"] = d.get("likes_count") or 0
        d["liked"] = bool(d.get("liked_by_me"))
        can = (viewer == d.get("profile_id")) or (album and viewer == owner)
        if album and album["group_id"] and not can:
            role = conn.execute(
                "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
                (album["group_id"], viewer)).fetchone()
            can = bool(role and role["role"] in ("owner", "admin", "moderator"))
        d["can_edit"] = can
        d["can_delete"] = can
        out.append(d)
    return JSONResponse(out)

@router.post("/albums/{album_id}/photos/upload")
def upload_album_photo(album_id: str, request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    album = conn.execute("SELECT * FROM social_albums WHERE id=? AND profile_id=?", (album_id, profile_id)).fetchone()
    if not album:
        raise HTTPException(404, "Альбом не найден")
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат")
    filename = f"{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    photo_id = str(uuid.uuid4())
    url = f"/api/social/file/root/{filename}"
    now = _now()
    conn.execute(
        "INSERT INTO social_profile_photos (id, profile_id, url, caption, created_at) VALUES (?,?,?,?,?)",
        (photo_id, profile_id, url, "", now)
    )
    conn.execute(
        "INSERT OR IGNORE INTO social_album_photos (album_id, photo_id, sort_order, added_at) VALUES (?,?,?,?)",
        (album_id, photo_id, int(time.time()), now)
    )
    count = conn.execute("SELECT COUNT(*) FROM social_album_photos WHERE album_id=?", (album_id,)).fetchone()[0]
    conn.execute("UPDATE social_albums SET photo_count=? WHERE id=?", (count, album_id))
    if not album["cover_path"]:
        conn.execute("UPDATE social_albums SET cover_path=? WHERE id=?", (url, album_id))
    conn.commit()
    return JSONResponse({"ok": True, "photo_id": photo_id, "url": url})

@router.delete("/albums/{album_id}/photos/{photo_id}")
def remove_album_photo(album_id: str, photo_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    conn.execute("DELETE FROM social_album_photos WHERE album_id=? AND photo_id=?", (album_id, photo_id))
    count = conn.execute("SELECT COUNT(*) FROM social_album_photos WHERE album_id=?", (album_id,)).fetchone()[0]
    conn.execute("UPDATE social_albums SET photo_count=? WHERE id=?", (count, album_id))
    if count == 0:
        conn.execute("UPDATE social_albums SET cover_path='' WHERE id=?", (album_id,))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Photo viewer: likes & comments ---

def _photo_album(conn, photo_id):
    """Альбом, в котором лежит фото, или None.

    Фото может принадлежать сразу нескольким альбомам (social_album_photos
    допускает это), поэтому берём первый — для проверки прав достаточно
    знать, что альбом вообще существует.
    """
    return conn.execute(
        "SELECT a.* FROM social_albums a "
        "JOIN social_album_photos ap ON ap.album_id = a.id "
        "WHERE ap.photo_id=? LIMIT 1", (photo_id,)).fetchone()


def _photo_view_allowed(conn, photo, viewer):
    """Может ли viewer видеть/комментировать это фото.

    Фото из альбома группы видно только участникам группы, из альбома
    канала — только участникам сервера с доступом в канал. Раньше такой
    проверки не было вовсе: комментарий к фото чужой группы можно было
    оставить, зная photo_id.
    """
    album = _photo_album(conn, photo["id"])
    if not album:
        return True                       # фото профиля — прежнее поведение
    if album["channel_id"]:
        from social_scope import channel_scope
        try:
            channel_scope(conn, album["channel_id"], album["channel_id"], viewer)
        except HTTPException:
            swallow.note('social_module._photo_view_allowed:L3916')
            return False
        return True
    if album["group_id"]:
        member = conn.execute(
            "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?",
            (album["group_id"], viewer)).fetchone()
        return bool(member)
    return True


def _require_photo_comment_access(conn, photo_id, viewer):
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    if not _photo_view_allowed(conn, photo, viewer):
        raise HTTPException(403, "Нет доступа к этому фото")
    return photo


def _photo_comment_rows(conn, photo_id, offset, limit):
    return conn.execute(
        "SELECT c.*, COALESCE(NULLIF(c.content,''), c.text) AS content, "
        "       COALESCE(u.display_name, c.profile_id) AS display_name, "
        "       u.avatar_path AS avatar_path "
        "FROM social_photo_comments c "
        "LEFT JOIN social_users u ON u.profile_id = c.profile_id "
        "WHERE c.photo_id=? ORDER BY c.created_at ASC LIMIT ? OFFSET ?",
        (photo_id, limit, offset)).fetchall()


def _photo_comment_dict(row, viewer):
    cd = _row_to_dict(row)
    cd["content"] = cd.get("content") or cd.get("text") or ""
    cd["display_name"] = cd.get("display_name") or _get_profile_name(cd["profile_id"])
    liked = False
    if cd.get("likes_count") and cd["profile_id"] != viewer:
        liked = _get_db().execute(
            "SELECT 1 FROM social_photo_comment_likes WHERE comment_id=? AND profile_id=?",
            (cd["id"], viewer)).fetchone() is not None
    cd["liked_by_me"] = liked
    cd["can_delete"] = cd["profile_id"] == viewer
    return cd


@router.get("/photos/{photo_id}")
def get_photo_details(photo_id: str, request: Request):
    viewer = _get_active_profile(request)
    conn = _get_db()
    photo = _require_photo_comment_access(conn, photo_id, viewer)
    data = _row_to_dict(photo)
    data["likes_count"] = conn.execute("SELECT COUNT(*) FROM social_photo_likes WHERE photo_id=?", (photo_id,)).fetchone()[0]
    data["liked_by_me"] = conn.execute(
        "SELECT 1 FROM social_photo_likes WHERE photo_id=? AND profile_id=?", (photo_id, viewer)
    ).fetchone() is not None
    data["comments_count"] = conn.execute(
        "SELECT COUNT(*) FROM social_photo_comments WHERE photo_id=?", (photo_id,)).fetchone()[0]
    comments = conn.execute(
        "SELECT c.*, COALESCE(NULLIF(c.content,''), c.text) AS content, "
        "       u.display_name, u.avatar_path FROM social_photo_comments c "
        "LEFT JOIN social_users u ON u.profile_id=c.profile_id WHERE c.photo_id=? "
        "ORDER BY c.created_at ASC LIMIT 50",
        (photo_id,)
    ).fetchall()
    data["comments"] = [_photo_comment_dict(c, viewer) for c in comments]
    tags = conn.execute(
        "SELECT t.*, u.display_name, u.avatar_path FROM social_photo_tags t "
        "LEFT JOIN social_users u ON u.profile_id = t.profile_id WHERE t.photo_id=? ORDER BY t.created_at ASC",
        (photo_id,)
    ).fetchall()
    data["tags"] = []
    for t in tags:
        td = _row_to_dict(t)
        td["display_name"] = td.get("display_name") or td.get("name") or td.get("profile_id")
        data["tags"].append(td)
    return JSONResponse(data)

@router.get("/photos/{photo_id}/comments")
def list_photo_comments(photo_id: str, request: Request,
                              offset: int = 0, limit: int = 50):
    """Комментарии к фото постранично.

    Раньше они приезжали только вложенными в GET /photos/{id}, поэтому
    подгрузить вторую страницу было невозможно, а полный список тянулся
    целиком вместе с тегами и счётчиками.
    """
    viewer = _get_active_profile(request)
    conn = _get_db()
    _require_photo_comment_access(conn, photo_id, viewer)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    rows = _photo_comment_rows(conn, photo_id, offset, limit)
    total = conn.execute(
        "SELECT COUNT(*) FROM social_photo_comments WHERE photo_id=?", (photo_id,)).fetchone()[0]
    return JSONResponse({
        "comments": [_photo_comment_dict(r, viewer) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
    })

@router.post("/photos/{photo_id}/like")
def toggle_photo_like(photo_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not conn.execute("SELECT 1 FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone():
        raise HTTPException(404, "Фото не найдено")
    existing = conn.execute("SELECT 1 FROM social_photo_likes WHERE photo_id=? AND profile_id=?", (photo_id, profile_id)).fetchone()
    if existing:
        conn.execute("DELETE FROM social_photo_likes WHERE photo_id=? AND profile_id=?", (photo_id, profile_id))
        liked = False
    else:
        conn.execute("INSERT OR IGNORE INTO social_photo_likes (photo_id, profile_id, created_at) VALUES (?,?,?)", (photo_id, profile_id, _now()))
        liked = True
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM social_photo_likes WHERE photo_id=?", (photo_id,)).fetchone()[0]
    return JSONResponse({"ok": True, "liked": liked, "likes_count": count})

@router.post("/photos/{photo_id}/comments")
def add_photo_comment(photo_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    text = (data.get("text") or data.get("content") or "").strip()
    if not text:
        raise HTTPException(400, "Комментарий не может быть пустым")
    conn = _get_db()
    _require_photo_comment_access(conn, photo_id, profile_id)
    parent_id = (data.get("parent_id") or "").strip()
    if parent_id:
        # Ответ обязан быть на комментарий этого же фото, иначе через
        # подделанный parent_id можно было бы привязать его к чужой ветке.
        ok = conn.execute(
            "SELECT 1 FROM social_photo_comments WHERE id=? AND photo_id=?",
            (parent_id, photo_id)).fetchone()
        if not ok:
            parent_id = ""
    comment_id = str(uuid.uuid4())
    # Пишем и в content, и в text: старое поле осталось в схеме, а часть
    # страниц до сих пор читает именно его.
    conn.execute(
        "INSERT INTO social_photo_comments (id, photo_id, profile_id, text, content, parent_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (comment_id, photo_id, profile_id, text[:2000], text[:2000], parent_id, _now())
    )
    # Упоминания разбираются здесь же: раньше комментарии не поддерживали
    # @ вообще, хотя колонка social_mentions.comment_id для них заведена.
    _parse_mentions(text[:2000], comment_id, profile_id, conn, "photo", photo_id)
    conn.commit()
    c = conn.execute(
        "SELECT c.*, COALESCE(NULLIF(c.content,''), c.text) AS content, "
        "       u.display_name, u.avatar_path FROM social_photo_comments c "
        "LEFT JOIN social_users u ON u.profile_id=c.profile_id WHERE c.id=?",
        (comment_id,)
    ).fetchone()
    return JSONResponse({"ok": True, "comment": _photo_comment_dict(c, profile_id)})

@router.put("/photos/{photo_id}/comments/{comment_id}")
def update_photo_comment(photo_id: str, comment_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    comment = conn.execute(
        "SELECT * FROM social_photo_comments WHERE id=? AND photo_id=?", (comment_id, photo_id)
    ).fetchone()
    if not comment:
        raise HTTPException(404, "Комментарий не найден")
    if comment["profile_id"] != profile_id:
        raise HTTPException(403, "Можно править только свой комментарий")
    text = (data.get("text") or data.get("content") or "").strip()
    if not text:
        raise HTTPException(400, "Комментарий не может быть пустым")
    conn.execute(
        "UPDATE social_photo_comments SET text=?, content=?, edited_at=? WHERE id=?",
        (text[:2000], text[:2000], _now(), comment_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/photos/comments/{comment_id}/like")
def toggle_photo_comment_like(comment_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    comment = conn.execute("SELECT * FROM social_photo_comments WHERE id=?", (comment_id,)).fetchone()
    if not comment:
        raise HTTPException(404, "Комментарий не найден")
    _require_photo_comment_access(conn, comment["photo_id"], profile_id)
    existing = conn.execute(
        "SELECT 1 FROM social_photo_comment_likes WHERE comment_id=? AND profile_id=?",
        (comment_id, profile_id)).fetchone()
    if existing:
        conn.execute(
            "DELETE FROM social_photo_comment_likes WHERE comment_id=? AND profile_id=?",
            (comment_id, profile_id))
        liked = False
    else:
        conn.execute(
            "INSERT INTO social_photo_comment_likes (comment_id, profile_id, created_at) VALUES (?,?,?)",
            (comment_id, profile_id, _now()))
        liked = True
    conn.execute(
        "UPDATE social_photo_comments SET likes_count="
        "(SELECT COUNT(*) FROM social_photo_comment_likes WHERE comment_id=?) WHERE id=?",
        (comment_id, comment_id))
    conn.commit()
    count = conn.execute(
        "SELECT likes_count FROM social_photo_comments WHERE id=?", (comment_id,)).fetchone()[0]
    return JSONResponse({"ok": True, "liked": liked, "likes_count": count})

@router.delete("/photos/{photo_id}/comments/{comment_id}")
def delete_photo_comment(photo_id: str, comment_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    comment = conn.execute("SELECT * FROM social_photo_comments WHERE id=?", (comment_id,)).fetchone()
    if not comment:
        raise HTTPException(404, "Комментарий не найден")
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone()
    album = _photo_album(conn, photo_id)
    is_manager = False
    if album and album["group_id"]:
        is_manager = bool(conn.execute(
            "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=? AND role IN ('admin','moderator','owner')",
            (album["group_id"], profile_id)).fetchone())
    if comment["profile_id"] != profile_id and not is_manager \
            and (not photo or photo["profile_id"] != profile_id):
        raise HTTPException(403, "Нельзя удалить чужой комментарий")
    conn.execute("DELETE FROM social_photo_comments WHERE id=?", (comment_id,))
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/photos/{photo_id}/tags")
def get_photo_tags(photo_id: str, request: Request):
    viewer = _get_active_profile(request)
    conn = _get_db()
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    if photo["profile_id"] != viewer:
        _ensure_user(viewer)
    tags = conn.execute(
        "SELECT t.*, u.display_name, u.avatar_path FROM social_photo_tags t "
        "LEFT JOIN social_users u ON u.profile_id = t.profile_id WHERE t.photo_id=? ORDER BY t.created_at ASC",
        (photo_id,)
    ).fetchall()
    result = []
    for t in tags:
        td = _row_to_dict(t)
        td["display_name"] = td.get("display_name") or td.get("name") or td.get("profile_id")
        result.append(td)
    return JSONResponse(result)

@router.put("/photos/{photo_id}/tags")
def add_photo_tag(photo_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        raise HTTPException(404, "Фото не найдено")
    if photo["profile_id"] != profile_id:
        raise HTTPException(403, "Теги может добавлять только владелец фото")
    target = data.get("profile_id", "")
    if target:
        _ensure_user(target)
    name = (data.get("name") or "").strip()[:80]
    tag_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_photo_tags (id, photo_id, profile_id, name, x, y, w, h, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (tag_id, photo_id, target, name, float(data.get("x", 0)), float(data.get("y", 0)),
         float(data.get("w", 0)), float(data.get("h", 0)), _now())
    )
    conn.commit()
    return JSONResponse({"ok": True, "id": tag_id})

@router.delete("/photos/{photo_id}/tags/{tag_id}")
def delete_photo_tag(photo_id: str, tag_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    photo = conn.execute("SELECT * FROM social_profile_photos WHERE id=?", (photo_id,)).fetchone()
    tag = conn.execute("SELECT * FROM social_photo_tags WHERE id=?", (tag_id,)).fetchone()
    if not photo or not tag or tag["photo_id"] != photo_id:
        raise HTTPException(404, "Тег не найден")
    if photo["profile_id"] != profile_id and tag["profile_id"] != profile_id:
        raise HTTPException(403, "Нельзя удалить чужой тег")
    conn.execute("DELETE FROM social_photo_tags WHERE id=?", (tag_id,))
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/groups")
def get_my_groups(request: Request, search: str = Query("")):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    base_sql = (
        "SELECT g.*, m.role AS my_role, m.joined_at AS my_joined_at,"
        " (SELECT COUNT(*) FROM social_group_members gm WHERE gm.group_id=g.id) AS member_count,"
        " (m.profile_id IS NOT NULL) AS is_member"
        " FROM social_groups g LEFT JOIN social_group_members m"
        " ON g.id = m.group_id AND m.profile_id = ?"
    )
    if search:
        pattern = f"%{search}%"
        rows = conn.execute(
            base_sql + " WHERE g.name LIKE ? OR g.description LIKE ? ORDER BY g.last_activity DESC, g.created_at DESC",
            (profile_id, pattern, pattern)
        ).fetchall()
    else:
        rows = conn.execute(
            base_sql + " ORDER BY g.last_activity DESC, g.created_at DESC",
            (profile_id,)
        ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

@router.get("/groups/discover")
def discover_groups(request: Request, search: str = Query(""), offset: int = Query(0), limit: int = Query(20), category: str = Query("")):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    base_sql = (
        "SELECT g.*, m.role AS my_role,"
        " (SELECT COUNT(*) FROM social_group_members gm WHERE gm.group_id=g.id) AS member_count,"
        " (m.profile_id IS NOT NULL) AS is_member"
        " FROM social_groups g LEFT JOIN social_group_members m"
        " ON g.id = m.group_id AND m.profile_id = ?"
        " WHERE g.privacy='public'"
    )
    params = [profile_id]
    if category:
        base_sql += " AND g.category = ?"
        params.append(category)
    if search:
        pattern = f"%{search}%"
        base_sql += " AND (g.name LIKE ? OR g.description LIKE ? OR g.tags LIKE ?)"
        params.extend([pattern, pattern, pattern])
    base_sql += " ORDER BY member_count DESC, g.last_activity DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(base_sql, params).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])


@router.get("/groups/categories")
def group_categories(request: Request):
    conn = _get_db()
    rows = conn.execute(
        "SELECT category, COUNT(*) AS cnt FROM social_groups"
        " WHERE privacy='public' AND category != '' GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    return JSONResponse([{"name": r["category"], "count": r["cnt"]} for r in rows])

@router.get("/groups/{group_id}")
def get_group(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    group = conn.execute("SELECT g.*, (SELECT COUNT(*) FROM social_group_members WHERE group_id=g.id) AS member_cnt FROM social_groups g WHERE g.id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    result = _row_to_dict(group)
    membership = conn.execute("SELECT * FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    result["my_role"] = membership["role"] if membership else ""
    result["is_member"] = membership is not None
    # Get last 5 members for preview
    members = conn.execute(
        "SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_members m JOIN social_users u ON m.profile_id=u.profile_id WHERE m.group_id=? ORDER BY m.joined_at DESC LIMIT 5",
        (group_id,)
    ).fetchall()
    result["members_preview"] = [_row_to_dict(m) for m in members]
    return JSONResponse(result)

@router.post("/groups")
def create_group(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    if not _check_rate_limit(f"group:{profile_id}", 10, 60):
        raise HTTPException(429, "Слишком много групп. Подождите минуту")
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Название обязательно")
    if len(name) > 64:
        raise HTTPException(400, "Название слишком длинное (максимум 64 символа)")
    description = (data.get("description") or "").strip()[:1000]
    group_id = str(uuid.uuid4())
    now = _now()
    privacy = data.get("privacy", "public")
    if privacy not in ("public", "closed", "private"):
        privacy = "public"
    is_channel = 1 if data.get("is_channel") in (1, True, "1", "true") else 0
    tags_raw = data.get("tags") or ""
    # Фронтенд может присылать теги как массив; раньше срез [:]300 применялся
    # до проверки типа и падал с TypeError на числе/объекте.
    if isinstance(tags_raw, (list, tuple)):
        tags = ", ".join(str(t).strip() for t in tags_raw if str(t).strip())[:300]
    elif isinstance(tags_raw, str):
        tags = tags_raw[:300]
    else:
        tags = ""
    category = (data.get("category") or "")[:50]
    link = (data.get("link") or "")[:200]
    post_permission = data.get("post_permission", 2 if is_channel else 0)
    if post_permission not in (0, 1, 2):
        post_permission = 2 if is_channel else 0
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_groups (id, name, description, owner_id, created_at, member_count,"
        " privacy, is_channel, category, tags, last_activity, link, post_permission)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (group_id, name, description, profile_id, now, 1,
         privacy, is_channel, category, tags, now, link, post_permission)
    )
    conn.execute(
        "INSERT INTO social_group_members (group_id, profile_id, role, joined_at) VALUES (?,?,'admin',?)",
        (group_id, profile_id, now)
    )
    conn.commit()
    return JSONResponse({"id": group_id, "ok": True, "name": name, "is_channel": bool(is_channel)})

@router.put("/groups/{group_id}")
def update_group(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    # Валидируем то же, что и при создании: иначе через PUT можно записать
    # пустое/слишком длинное название или неизвестный тип приватности.
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Название обязательно")
        if len(name) > 64:
            raise HTTPException(400, "Название слишком длинное (максимум 64 символа)")
        data["name"] = name
    if "privacy" in data and data["privacy"] not in ("public", "closed", "private"):
        raise HTTPException(400, "Недопустимый тип приватности")
    if "post_permission" in data:
        try:
            pp = int(data["post_permission"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Недопустимые права публикации")
        if pp not in (0, 1, 2):
            raise HTTPException(400, "Недопустимые права публикации")
        data["post_permission"] = pp
    if "tags" in data:
        tv = data.get("tags") or ""
        if isinstance(tv, (list, tuple)):
            tv = ", ".join(str(t).strip() for t in tv if str(t).strip())
        data["tags"] = str(tv)[:300]
    if "category" in data:
        data["category"] = str(data.get("category") or "")[:50]
    if "link" in data:
        data["link"] = str(data.get("link") or "")[:200]
    updates = []
    params = []
    for field in ("name", "description", "privacy", "is_channel", "category", "tags", "link", "post_permission"):
        if field in data:
            updates.append(f"{field}=?")
            params.append(data[field])
    if updates:
        params.append(group_id)
        conn.execute(f"UPDATE social_groups SET {','.join(updates)} WHERE id=?", params)
        conn.commit()
    return JSONResponse({"ok": True})

@router.post("/groups/{group_id}/join")
def join_group(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"join_group:{profile_id}", 10, 60):
        raise HTTPException(429, "Слишком много вступлений в группы.")
    conn = _get_db()
    group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    if group["privacy"] == "private":
        raise HTTPException(403, "Группа закрыта")
    banned = conn.execute("SELECT 1 FROM social_group_bans WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if banned:
        raise HTTPException(403, "Вы забанены в этой группе")
    existing = conn.execute("SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if existing:
        return JSONResponse({"ok": True, "status": "already_member"})
    # Закрытая группа/канал: вступление только по заявке, модератору решает
    if group["privacy"] == "closed":
        pending = conn.execute(
            "SELECT 1 FROM social_group_join_requests WHERE group_id=? AND profile_id=? AND status='pending'",
            (group_id, profile_id)
        ).fetchone()
        if pending:
            return JSONResponse({"ok": True, "status": "pending"})
        conn.execute(
            "INSERT INTO social_group_join_requests (id, group_id, profile_id, message, status, created_at)"
            " VALUES (?,?,?,?, 'pending', ?)",
            (str(uuid.uuid4()), group_id, profile_id, "", _now())
        )
        conn.commit()
        return JSONResponse({"ok": True, "status": "pending"})
    conn.execute(
        "INSERT INTO social_group_members (group_id, profile_id, role, joined_at) VALUES (?,?,'member',?)",
        (group_id, profile_id, _now())
    )
    conn.execute("UPDATE social_groups SET member_count = member_count + 1, last_activity=? WHERE id=?", (_now(), group_id))
    conn.commit()
    _create_notification(conn, group["owner_id"], "group_join", profile_id, "group", group_id)
    return JSONResponse({"ok": True})

@router.post("/groups/{group_id}/leave")
def leave_group(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership:
        raise HTTPException(400, "Вы не участник")
    if membership["role"] == "admin":
        raise HTTPException(400, "Администратор не может покинуть группу. Передайте права или удалите группу")
    conn.execute("DELETE FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id))
    conn.execute("UPDATE social_groups SET member_count = MAX(1, member_count - 1) WHERE id=?", (group_id,))
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/groups/{group_id}/members")
def get_group_members(group_id: str, offset: int = Query(0), limit: int = Query(50)):
    conn = _get_db()
    group = conn.execute("SELECT 1 FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    rows = conn.execute(
        "SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_online, u.last_seen FROM social_group_members m JOIN social_users u ON m.profile_id=u.profile_id WHERE m.group_id=? ORDER BY m.role='admin' DESC, m.joined_at ASC LIMIT ? OFFSET ?",
        (group_id, limit, offset)
    ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

@router.post("/groups/{group_id}/members/kick")
def kick_member(group_id: str, request: Request, data: dict = Body(...)):
    admin_id = _get_active_profile(request)
    target_id = data.get("profile_id", "")
    if not target_id:
        raise HTTPException(400, "Укажите profile_id")
    conn = _get_db()
    admin_role = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, admin_id)).fetchone()
    if not admin_role or admin_role["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    target_role = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, target_id)).fetchone()
    if not target_role:
        raise HTTPException(400, "Пользователь не в группе")
    group = conn.execute("SELECT owner_id FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if group and group["owner_id"] == target_id:
        raise HTTPException(400, "Нельзя кикнуть владельца группы")
    if target_role["role"] == "owner":
        raise HTTPException(400, "Нельзя кикнуть владельца группы")
    if target_role["role"] == "admin":
        raise HTTPException(400, "Нельзя кикнуть администратора")
    conn.execute("DELETE FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, target_id))
    conn.execute("UPDATE social_groups SET member_count = MAX(1, member_count - 1) WHERE id=?", (group_id,))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/groups/{group_id}/members/promote")
def promote_member(group_id: str, request: Request, data: dict = Body(...)):
    admin_id = _get_active_profile(request)
    target_id = data.get("profile_id", "")
    new_role = data.get("role", "moderator")
    if new_role not in ("admin", "moderator", "member"):
        raise HTTPException(400, "Недопустимая роль")
    conn = _get_db()
    admin_role = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, admin_id)).fetchone()
    if not admin_role or admin_role["role"] != "admin":
        raise HTTPException(403, "Только администратор")
    group = conn.execute("SELECT owner_id FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if group and group["owner_id"] == target_id:
        raise HTTPException(400, "Нельзя изменить роль владельца группы")
    target_role = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, target_id)).fetchone()
    if not target_role:
        raise HTTPException(404, "Пользователь не в группе")
    conn.execute("UPDATE social_group_members SET role=? WHERE group_id=? AND profile_id=?", (new_role, group_id, target_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.delete("/groups/{group_id}")
def delete_group(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    if group["owner_id"] != profile_id:
        raise HTTPException(403, "Только владелец может удалить группу")
    # Собираем id постов до удаления: после DELETE их уже не найти, а лайки,
    # комментарии и хэштеги остались бы сиротами.
    post_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM social_group_posts WHERE group_id=?", (group_id,)).fetchall()]
    if post_ids:
        marks = ",".join("?" * len(post_ids))
        conn.execute(f"DELETE FROM social_comments WHERE target_type='group_post' AND target_id IN ({marks})", post_ids)
        conn.execute(f"DELETE FROM social_likes WHERE target_type='group_post' AND target_id IN ({marks})", post_ids)
        conn.execute(f"DELETE FROM social_hashtags WHERE post_id IN ({marks})", post_ids)
    # Опросы и голоса
    conn.execute("DELETE FROM social_group_poll_votes WHERE poll_id IN (SELECT id FROM social_group_polls WHERE group_id=?)", (group_id,))
    # Форум: сначала лайки ответов, затем ответы, темы и разделы
    conn.execute("DELETE FROM social_forum_post_likes WHERE post_id IN (SELECT id FROM social_forum_posts WHERE group_id=?)", (group_id,))
    conn.execute("DELETE FROM social_forum_posts WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_forum_threads WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_forum_sections WHERE group_id=?", (group_id,))
    # Альбомы: удаляем связи и сами альбомы, но НЕ social_profile_photos —
    # фотография принадлежит пользователю и может быть в личном альбоме.
    conn.execute("DELETE FROM social_album_photos WHERE album_id IN (SELECT id FROM social_albums WHERE group_id=?)", (group_id,))
    conn.execute("DELETE FROM social_albums WHERE group_id=?", (group_id,))
    # Статьи и их разделы
    conn.execute("DELETE FROM social_article_sections WHERE article_id IN (SELECT id FROM social_articles WHERE group_id=?)", (group_id,))
    conn.execute("DELETE FROM social_articles WHERE group_id=?", (group_id,))
    # Чат, реакции, приглашения, заявки и баны
    conn.execute("DELETE FROM social_group_chat_reactions WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_chat_messages WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_invites WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_join_requests WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_bans WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_notifications WHERE target_type='group' AND target_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_posts WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_polls WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_group_members WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM social_groups WHERE id=?", (group_id,))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Group Posts ---

def _load_shareable(conn, item_id):
    """Найти объект, который можно репостнуть: пост ленты, пост группы
    или фото.

    Раньше repost_post читал только social_posts, поэтому фото альбома и
    пост группы отдавали 404, а поделиться ими было нечем.
    """
    if not item_id:
        return None
    row = conn.execute(
        "SELECT id, author_id, content, attachments, '' AS group_id, 'post' AS kind "
        "FROM social_posts WHERE id=?", (item_id,)).fetchone()
    if row:
        return _row_to_dict(row)
    row = conn.execute(
        "SELECT id, author_id, content, attachments, group_id, 'group_post' AS kind "
        "FROM social_group_posts WHERE id=?", (item_id,)).fetchone()
    if row:
        return _row_to_dict(row)
    row = conn.execute(
        "SELECT id, profile_id AS author_id, caption AS content, url AS attachments, "
        "       '' AS group_id, 'photo' AS kind "
        "FROM social_profile_photos WHERE id=?", (item_id,)).fetchone()
    if row:
        return _row_to_dict(row)
    return None


def _attach_repost(conn, post):
    """Развернуть repost_id в данные оригинала для клиента."""
    rid = post.get("repost_id")
    if not rid:
        return
    src = conn.execute(
        "SELECT p.id, p.author_id, p.content, p.created_at, u.display_name, u.avatar_path "
        "FROM social_posts p JOIN social_users u ON u.profile_id = p.author_id WHERE p.id=?",
        (rid,)).fetchone()
    if src:
        d = _row_to_dict(src)
        post["repost_author_name"] = d.get("display_name") or d.get("author_id")
        post["repost_author_id"] = d.get("author_id")
        post["repost_author_avatar"] = d.get("avatar_path") or ""
        post["repost_content"] = (d.get("content") or "")[:200]
        post["repost_type"] = "post"
        return
    src = conn.execute(
        "SELECT p.id, p.author_id, p.content, p.group_id, p.created_at, "
        "       u.display_name, u.avatar_path "
        "FROM social_group_posts p JOIN social_users u ON u.profile_id = p.author_id WHERE p.id=?",
        (rid,)).fetchone()
    if src:
        d = _row_to_dict(src)
        post["repost_author_name"] = d.get("display_name") or d.get("author_id")
        post["repost_author_id"] = d.get("author_id")
        post["repost_author_avatar"] = d.get("avatar_path") or ""
        post["repost_content"] = (d.get("content") or "")[:200]
        post["repost_type"] = "group_post"
        post["repost_group_id"] = d.get("group_id") or ""
        return
    # Фото: показываем саму картинку в карточке репоста.
    src = conn.execute(
        "SELECT id, profile_id AS author_id, caption, url FROM social_profile_photos WHERE id=?",
        (rid,)).fetchone()
    if src:
        d = _row_to_dict(src)
        post["repost_author_id"] = d.get("author_id")
        post["repost_content"] = d.get("caption") or ""
        post["repost_type"] = "photo"
        post["repost_photo_url"] = d.get("url") or ""

@router.get("/groups/{group_id}/posts")
def get_group_posts(group_id: str, request: Request, offset: int = Query(0), limit: int = Query(20), search: str = Query("")):
    conn = _get_db()
    group = conn.execute("SELECT 1 FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    profile_id = _get_active_profile(request) if request else ""
    where = "p.group_id=?"
    params = [group_id]
    if search:
        where += " AND p.content LIKE ?"
        params.append(f"%{search}%")
    rows = conn.execute(
        f"SELECT p.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_posts p JOIN social_users u ON p.author_id=u.profile_id WHERE {where} ORDER BY p.is_pinned DESC, p.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset)
    ).fetchall()
    posts = []
    for r in rows:
        post = _row_to_dict(r)
        liked = conn.execute(
            "SELECT 1 FROM social_likes WHERE user_id=? AND target_type='group_post' AND target_id=?",
            (profile_id, r["id"])
        ).fetchone()
        post["liked_by_me"] = liked is not None
        post["comments_count"] = conn.execute(
            "SELECT COUNT(*) FROM social_comments WHERE target_type='group_post' AND target_id=?",
            (r["id"],)
        ).fetchone()[0]
        try:
            post["attachments"] = json.loads(post.get("attachments", "[]"))
        except Exception:
            swallow.note('social_module.get_group_posts:L4637')
            post["attachments"] = []
        if post.get("poll_id"):
            poll = conn.execute("SELECT * FROM social_group_polls WHERE id=?", (post["poll_id"],)).fetchone()
            if poll:
                p = _row_to_dict(poll)
                p["options"] = json.loads(p["options"])
                my_vote = conn.execute("SELECT option_index FROM social_group_poll_votes WHERE poll_id=? AND profile_id=?", (poll["id"], profile_id)).fetchone()
                p["my_vote"] = my_vote["option_index"] if my_vote else -1
                votes = conn.execute("SELECT option_index, COUNT(*) as cnt FROM social_group_poll_votes WHERE poll_id=? GROUP BY option_index", (poll["id"],)).fetchall()
                p["votes"] = {str(v["option_index"]): v["cnt"] for v in votes}
                p["total_votes"] = sum(v["cnt"] for v in votes)
                post["poll"] = p
        _attach_repost(conn, post)
        posts.append(post)
    return JSONResponse(posts)

@router.post("/groups/{group_id}/posts")
async def create_group_post(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"gpost:{profile_id}", 15, 60):
        raise HTTPException(429, "Слишком много постов.")
    content = data.get("content", "").strip()
    attachments_raw = data.get("attachments", "[]")
    if isinstance(attachments_raw, list):
        attachments_raw = json.dumps(attachments_raw)
    # Проверяем именно разобранные вложения. Раньше здесь стояло
    # `not attachments_raw`, а это строка, и строка "[]" истинна — проверка
    # пропускала пост без текста и без вложений.
    attachments = _safe_attachments(attachments_raw)
    if not content and attachments == "[]":
        raise HTTPException(400, "Текст обязателен")
    conn = _get_db()
    group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership:
        raise HTTPException(403, "Вы не участник группы")
    post_perm = group["post_permission"] if group["post_permission"] is not None else 0
    if group["is_channel"] and membership["role"] != "admin":
        raise HTTPException(403, "Только администратор может писать в канале")
    if post_perm == 2 and membership["role"] not in ("admin",):
        raise HTTPException(403, "Только администратор может писать")
    if post_perm == 1 and membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Только администраторы и модераторы могут писать")
    post_id = str(uuid.uuid4())
    now = _now()
    poll_id = str(data.get("poll_id") or "").strip()
    if poll_id:
        poll = conn.execute(
            "SELECT id FROM social_group_polls WHERE id=? AND group_id=?",
            (poll_id, group_id)
        ).fetchone()
        if not poll:
            raise HTTPException(400, "Опрос не найден")
    # Репост: ссылка на исходник хранится отдельно от текста и вложений.
    repost_id = str(data.get("repost_id") or "").strip() or None
    repost_group_id = ""
    if repost_id:
        src = _load_shareable(conn, repost_id)
        if not src:
            raise HTTPException(404, "Источник репоста не найден")
        repost_group_id = src.get("group_id", "") or ""
    conn.execute(
        "INSERT INTO social_group_posts (id, group_id, author_id, content, created_at, attachments, poll_id, repost_id, repost_group_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (post_id, group_id, profile_id, content, now, attachments, poll_id, repost_id, repost_group_id)
    )
    _store_laya_triage(conn, "social_group_posts", post_id, _laya_triage_content(content))
    conn.execute("UPDATE social_groups SET last_activity=? WHERE id=?", (now, group_id))
    _parse_hashtags(content, post_id, conn)
    _parse_mentions(content, post_id, profile_id, conn)
    conn.commit()
    # Notify all members except author
    members = conn.execute(
        "SELECT profile_id FROM social_group_members WHERE group_id=? AND profile_id!=?", (group_id, profile_id)
    ).fetchall()
    for m in members:
        _create_notification(conn, m["profile_id"], "group_post", profile_id, "group", group_id)
    conn.commit()
    # Живая стена: без этого новый пост у остальных появлялся только после
    # перезагрузки страницы группы.
    try:
        await manager.broadcast_to_group(group_id, {
            "type": "group_post", "group_id": group_id, "post_id": post_id,
            "author_id": profile_id, "content": content[:120],
        }, exclude_profile_id=profile_id)
    except Exception:
        swallow.note('social_module.create_group_post:L4726')
        pass
    return JSONResponse({"id": post_id, "ok": True})

@router.get("/groups/{group_id}/posts/{post_id}")
def get_group_post(group_id: str, post_id: str):
    conn = _get_db()
    post = conn.execute(
        "SELECT p.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_posts p JOIN social_users u ON p.author_id=u.profile_id WHERE p.id=? AND p.group_id=?",
        (post_id, group_id)
    ).fetchone()
    if not post:
        raise HTTPException(404, "Пост не найден")
    result = _row_to_dict(post)
    try:
        result["attachments"] = json.loads(result.get("attachments", "[]"))
    except Exception:
        swallow.note('social_module.get_group_post:L4743')
        result["attachments"] = []
    return JSONResponse(result)

@router.delete("/groups/{group_id}/posts/{post_id}")
def delete_group_post(group_id: str, post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    post = conn.execute("SELECT * FROM social_group_posts WHERE id=? AND group_id=?", (post_id, group_id)).fetchone()
    if not post:
        raise HTTPException(404, "Пост не найден")
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if post["author_id"] != profile_id and (not membership or membership["role"] not in _GROUP_MOD_ROLES):
        raise HTTPException(403, "Нет прав")
    conn.execute("DELETE FROM social_comments WHERE target_type='group_post' AND target_id=?", (post_id,))
    conn.execute("DELETE FROM social_group_posts WHERE id=?", (post_id,))
    conn.commit()
    return JSONResponse({"ok": True})

@router.put("/groups/{group_id}/posts/{post_id}")
def update_group_post(group_id: str, post_id: str, request: Request, data: dict = Body(...)):
    """Редактирование поста в группе (автор или модератор/админ)."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    post = conn.execute("SELECT * FROM social_group_posts WHERE id=? AND group_id=?", (post_id, group_id)).fetchone()
    if not post:
        raise HTTPException(404, "Пост не найден")
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if post["author_id"] != profile_id and (not membership or membership["role"] not in _GROUP_MOD_ROLES):
        raise HTTPException(403, "Нет прав")
    content = (data.get("content") or "").strip()
    attachments = data.get("attachments")
    if content:
        conn.execute("UPDATE social_group_posts SET content=? WHERE id=?", (content, post_id))
        _parse_hashtags(content, post_id, conn)
    if attachments is not None:
        if isinstance(attachments, list):
            attachments = json.dumps(attachments)
        conn.execute("UPDATE social_group_posts SET attachments=? WHERE id=?", (attachments, post_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/groups/{group_id}/posts/{post_id}/like")
def toggle_group_post_like(group_id: str, post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    existing = conn.execute(
        "SELECT id FROM social_likes WHERE user_id=? AND target_type='group_post' AND target_id=?",
        (profile_id, post_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM social_likes WHERE id=?", (existing["id"],))
        conn.execute("UPDATE social_group_posts SET likes_count = MAX(0, likes_count - 1) WHERE id=?", (post_id,))
        conn.commit()
        return JSONResponse({"liked": False})
    conn.execute(
        "INSERT INTO social_likes (id, user_id, target_type, target_id, created_at) VALUES (?,?,'group_post',?,?)",
        (str(uuid.uuid4()), profile_id, post_id, _now())
    )
    conn.execute("UPDATE social_group_posts SET likes_count = likes_count + 1 WHERE id=?", (post_id,))
    conn.commit()
    return JSONResponse({"liked": True})

@router.post("/groups/{group_id}/posts/{post_id}/pin")
def toggle_pin_group_post(group_id: str, post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    post = conn.execute("SELECT is_pinned FROM social_group_posts WHERE id=? AND group_id=?", (post_id, group_id)).fetchone()
    if not post:
        raise HTTPException(404, "Пост не найден")
    new_val = 0 if post["is_pinned"] else 1
    conn.execute("UPDATE social_group_posts SET is_pinned=? WHERE id=?", (new_val, post_id))
    conn.commit()
    return JSONResponse({"is_pinned": new_val})

# --- Polls ---

@router.post("/groups/{group_id}/polls")
def create_group_poll(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    question = data.get("question", "").strip()
    options = data.get("options", [])
    if not question or len(options) < 2:
        raise HTTPException(400, "Нужен вопрос и минимум 2 варианта")
    poll_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO social_group_polls (id, group_id, question, options, created_at) VALUES (?,?,?,?,?)",
        (poll_id, group_id, question, json.dumps(options), now)
    )
    conn.commit()
    return JSONResponse({"id": poll_id, "ok": True})

@router.post("/groups/{group_id}/polls/{poll_id}/vote")
def vote_group_poll(group_id: str, poll_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    option_index = data.get("option", 0)
    conn = _get_db()
    membership = conn.execute("SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership:
        raise HTTPException(403, "Вы не участник")
    poll = conn.execute("SELECT * FROM social_group_polls WHERE id=? AND group_id=?", (poll_id, group_id)).fetchone()
    if not poll:
        raise HTTPException(404, "Опрос не найден")
    existing = conn.execute("SELECT 1 FROM social_group_poll_votes WHERE poll_id=? AND profile_id=?", (poll_id, profile_id)).fetchone()
    if existing:
        conn.execute("UPDATE social_group_poll_votes SET option_index=? WHERE poll_id=? AND profile_id=?", (option_index, poll_id, profile_id))
    else:
        conn.execute("INSERT INTO social_group_poll_votes (poll_id, profile_id, option_index, created_at) VALUES (?,?,?,?)", (poll_id, profile_id, option_index, _now()))
    conn.commit()
    return JSONResponse({"ok": True})


# --- Group invites ---

@router.post("/groups/{group_id}/invites")
def create_group_invite(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Создавать приглашения могут администраторы")
    role = data.get("role", "member")
    if role not in ("member", "moderator"):
        role = "member"
    try:
        max_uses = int(data.get("max_uses", 0) or 0)
    except (TypeError, ValueError):
        swallow.note('social_module.create_group_invite:L4880')
        max_uses = 0
    try:
        expires_days = int(data.get("expires_days", 0) or 0)
    except (TypeError, ValueError):
        swallow.note('social_module.create_group_invite:L4885')
        expires_days = 0
    expires_at = _now() + expires_days * 86400 if expires_days > 0 else 0
    code = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO social_group_invites (code, group_id, created_by, role, uses, max_uses, expires_at, created_at)"
        " VALUES (?,?,?,?,0,?,?,?)",
        (code, group_id, profile_id, role, max_uses, expires_at, _now())
    )
    conn.commit()
    return JSONResponse({"ok": True, "code": code,
                         "link": f"/social/friends?group={group_id}&invite={code}"})


@router.get("/groups/{group_id}/invites")
def list_group_invites(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    rows = conn.execute(
        "SELECT * FROM social_group_invites WHERE group_id=? ORDER BY created_at DESC", (group_id,)
    ).fetchall()
    result = []
    for r in rows:
        item = _row_to_dict(r)
        item["link"] = f"/social/friends?group={group_id}&invite={item['code']}"
        result.append(item)
    return JSONResponse(result)


@router.delete("/groups/{group_id}/invites/{code}")
def revoke_group_invite(group_id: str, code: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    conn.execute("DELETE FROM social_group_invites WHERE group_id=? AND code=?", (group_id, code))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/groups/join-by-invite/{code}")
def join_by_invite(code: str, request: Request):
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    conn = _get_db()
    invite = conn.execute("SELECT * FROM social_group_invites WHERE code=?", (code,)).fetchone()
    if not invite:
        raise HTTPException(404, "Приглашение не найдено или истекло")
    if invite["expires_at"] and invite["expires_at"] < _now():
        raise HTTPException(410, "Срок действия приглашения истёк")
    if invite["max_uses"] and invite["uses"] >= invite["max_uses"]:
        raise HTTPException(410, "Приглашение больше не действует")
    group_id = invite["group_id"]
    banned = conn.execute(
        "SELECT 1 FROM social_group_bans WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if banned:
        raise HTTPException(403, "Вы забанены в этой группе")
    existing = conn.execute(
        "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO social_group_members (group_id, profile_id, role, joined_at) VALUES (?,?,?,?)",
            (group_id, profile_id, invite["role"] or "member", _now())
        )
        conn.execute(
            "UPDATE social_groups SET member_count = member_count + 1, last_activity=? WHERE id=?",
            (_now(), group_id)
        )
    conn.execute("UPDATE social_group_invites SET uses = uses + 1 WHERE code=?", (code,))
    conn.commit()
    return JSONResponse({"ok": True, "group_id": group_id, "already": bool(existing)})


# --- Group stats & views ---

@router.post("/groups/{group_id}/posts/{post_id}/view")
def view_group_post(group_id: str, post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    exists = conn.execute(
        "SELECT 1 FROM social_group_posts WHERE id=? AND group_id=?", (post_id, group_id)
    ).fetchone()
    if not exists:
        raise HTTPException(404, "Пост не найден")
    cur = conn.execute(
        "INSERT OR IGNORE INTO social_post_views (post_id, profile_id, viewed_at) VALUES (?,?,?)",
        (post_id, profile_id, _now())
    )
    if cur.rowcount:
        conn.execute("UPDATE social_group_posts SET views_count = views_count + 1 WHERE id=?", (post_id,))
        conn.commit()
    total = conn.execute("SELECT COUNT(*) AS c FROM social_post_views WHERE post_id=?", (post_id,)).fetchone()
    return JSONResponse({"ok": True, "views": total["c"]})


@router.get("/groups/{group_id}/stats")
def group_stats(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    members = conn.execute(
        "SELECT COUNT(*) AS c FROM social_group_members WHERE group_id=?", (group_id,)
    ).fetchone()["c"]
    posts = conn.execute(
        "SELECT COUNT(*) AS c FROM social_group_posts WHERE group_id=?", (group_id,)
    ).fetchone()["c"]
    views = conn.execute(
        "SELECT COALESCE(SUM(views_count),0) AS c FROM social_group_posts WHERE group_id=?", (group_id,)
    ).fetchone()["c"]
    posts_today = conn.execute(
        "SELECT COUNT(*) AS c FROM social_group_posts WHERE group_id=? AND created_at > ?",
        (group_id, _now() - 86400)
    ).fetchone()["c"]
    active_week = conn.execute(
        "SELECT COUNT(DISTINCT author_id) AS c FROM social_group_posts WHERE group_id=? AND created_at > ?",
        (group_id, _now() - 7 * 86400)
    ).fetchone()["c"]
    online = conn.execute(
        "SELECT COUNT(*) AS c FROM social_group_members m JOIN social_users u ON u.profile_id=m.profile_id"
        " WHERE m.group_id=? AND u.is_online=1",
        (group_id,)
    ).fetchone()["c"]
    role_row = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    return JSONResponse({
        "ok": True, "group_id": group_id, "members": members, "posts": posts,
        "views": views, "posts_today": posts_today, "active_week": active_week,
        "online": online, "is_channel": bool(group["is_channel"]),
        "is_member": role_row is not None, "my_role": role_row["role"] if role_row else "",
    })


# --- Join Requests ---

@router.post("/groups/{group_id}/join-request")
def request_join_group(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Группа не найдена")
    banned = conn.execute("SELECT 1 FROM social_group_bans WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if banned:
        raise HTTPException(403, "Вы забанены в этой группе")
    existing = conn.execute("SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if existing:
        raise HTTPException(400, "Вы уже участник")
    existing_req = conn.execute("SELECT 1 FROM social_group_join_requests WHERE group_id=? AND profile_id=? AND status='pending'", (group_id, profile_id)).fetchone()
    if existing_req:
        raise HTTPException(400, "Заявка уже отправлена")
    req_id = str(uuid.uuid4())
    msg = data.get("message", "")
    conn.execute(
        "INSERT INTO social_group_join_requests (id, group_id, profile_id, message, status, created_at) VALUES (?,?,?,?,'pending',?)",
        (req_id, group_id, profile_id, msg, _now())
    )
    _create_notification(conn, group["owner_id"], "group_join_request", profile_id, "group", group_id)
    conn.commit()
    return JSONResponse({"id": req_id, "ok": True})

@router.get("/groups/{group_id}/join-requests")
def get_join_requests(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    rows = conn.execute(
        "SELECT r.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_join_requests r JOIN social_users u ON r.profile_id=u.profile_id WHERE r.group_id=? AND r.status='pending' ORDER BY r.created_at DESC",
        (group_id,)
    ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

@router.post("/groups/{group_id}/join-requests/{req_id}/approve")
def approve_join_request(group_id: str, req_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    req = conn.execute("SELECT * FROM social_group_join_requests WHERE id=? AND group_id=? AND status='pending'", (req_id, group_id)).fetchone()
    if not req:
        raise HTTPException(404, "Заявка не найдена")
    conn.execute("UPDATE social_group_join_requests SET status='approved' WHERE id=?", (req_id,))
    # INSERT OR IGNORE: пользователь мог вступить сам, пока заявка висела в
    # очереди. Тогда не должно быть ни ошибки PK, ни двойного +1 к счётчику.
    cur = conn.execute(
        "INSERT OR IGNORE INTO social_group_members (group_id, profile_id, role, joined_at) VALUES (?,?,'member',?)",
        (group_id, req["profile_id"], _now())
    )
    if cur.rowcount:
        conn.execute("UPDATE social_groups SET member_count = member_count + 1 WHERE id=?", (group_id,))
    _create_notification(conn, req["profile_id"], "group_join_approved", profile_id, "group", group_id)
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/groups/{group_id}/join-requests/{req_id}/reject")
def reject_join_request(group_id: str, req_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    conn.execute("UPDATE social_group_join_requests SET status='rejected' WHERE id=? AND group_id=?", (req_id, group_id))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Bans ---

@router.post("/groups/{group_id}/ban")
def ban_from_group(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target_id = data.get("profile_id", "")
    reason = data.get("reason", "")
    if not target_id:
        raise HTTPException(400, "Не указан пользователь")
    if target_id == profile_id:
        raise HTTPException(400, "Нельзя забанить самого себя")
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    target = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, target_id)).fetchone()
    group = conn.execute("SELECT owner_id FROM social_groups WHERE id=?", (group_id,)).fetchone()
    if group and group["owner_id"] == target_id:
        raise HTTPException(400, "Нельзя забанить владельца группы")
    if target and target["role"] == "owner":
        raise HTTPException(400, "Нельзя забанить владельца группы")
    if target and target["role"] == "admin":
        raise HTTPException(400, "Нельзя забанить администратора")
    conn.execute("INSERT OR REPLACE INTO social_group_bans (group_id, profile_id, banned_by, reason, created_at) VALUES (?,?,?,?,?)", (group_id, target_id, profile_id, reason, _now()))
    conn.execute("DELETE FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, target_id))
    # Счётчик уменьшаем только если бан действительно кого-то выгнал:
    # раньше бан не-участника отнимал участника у группы.
    if target:
        conn.execute("UPDATE social_groups SET member_count = MAX(1, member_count - 1) WHERE id=?", (group_id,))
    conn.execute("DELETE FROM social_group_join_requests WHERE group_id=? AND profile_id=? AND status='pending'", (group_id, target_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/groups/{group_id}/unban")
def unban_from_group(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target_id = data.get("profile_id", "")
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    conn.execute("DELETE FROM social_group_bans WHERE group_id=? AND profile_id=?", (group_id, target_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.get("/groups/{group_id}/bans")
def get_group_bans(group_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    rows = conn.execute(
        "SELECT b.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_bans b JOIN social_users u ON b.profile_id=u.profile_id WHERE b.group_id=? ORDER BY b.created_at DESC",
        (group_id,)
    ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

# --- Group Chat ---

@router.get("/groups/{group_id}/chat")
def get_group_chat(group_id: str, request: Request, before: Optional[float] = Query(None), after: Optional[float] = Query(None), limit: int = Query(50)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership:
        raise HTTPException(403, "Вы не участник")
    chronological = False
    if after:
        # Догрузка новых сообщений для автообновления чата на клиенте.
        # Без неё клиент запрашивал before= и получал более старую страницу.
        rows = conn.execute(
            "SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_bot FROM social_group_chat_messages m JOIN social_users u ON m.sender_id=u.profile_id WHERE m.group_id=? AND m.created_at>? ORDER BY m.created_at ASC LIMIT ?",
            (group_id, after, limit)
        ).fetchall()
        # Ответ уже в хронологическом порядке — реверс испортил бы его.
        chronological = True
    elif before:
        rows = conn.execute(
            "SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_bot FROM social_group_chat_messages m JOIN social_users u ON m.sender_id=u.profile_id WHERE m.group_id=? AND m.created_at<? ORDER BY m.created_at DESC LIMIT ?",
            (group_id, before, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT m.*, u.display_name, u.avatar_path, u.avatar_crop, u.is_bot FROM social_group_chat_messages m JOIN social_users u ON m.sender_id=u.profile_id WHERE m.group_id=? ORDER BY m.created_at DESC LIMIT ?",
            (group_id, limit)
        ).fetchall()
    msgs = []
    for r in rows:
        m = _row_to_dict(r)
        try:
            m["attachments"] = json.loads(m.get("attachments", "[]"))
        except Exception:
            swallow.note('social_module.get_group_chat:L5198')
            m["attachments"] = []
        try:
            reacts = conn.execute(
                "SELECT r.emoji, r.profile_id, u.display_name FROM social_group_chat_reactions r"
                " LEFT JOIN social_users u ON u.profile_id=r.profile_id"
                " WHERE r.message_id=? ORDER BY r.created_at",
                (m["id"],)
            ).fetchall()
        except Exception:
            swallow.note('social_module.get_group_chat:L5208')
            reacts = []
        grouped = {}
        for rr in reacts:
            e = grouped.setdefault(rr["emoji"], {"emoji": rr["emoji"], "count": 0, "users": [], "me": False})
            e["count"] += 1
            if rr["display_name"]:
                e["users"].append(rr["display_name"])
            if rr["profile_id"] == profile_id:
                e["me"] = True
        m["reactions"] = list(grouped.values())
        msgs.append(m)
    if not chronological:
        msgs.reverse()
    return JSONResponse(msgs)


@router.post("/groups/{group_id}/chat/messages/{message_id}/reaction")
def toggle_group_chat_reaction(group_id: str, message_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    emoji = (data.get("emoji") or "").strip()[:16]
    if not emoji:
        raise HTTPException(400, "Эмодзи обязателен")
    conn = _get_db()
    membership = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if not membership:
        raise HTTPException(403, "Вы не участник")
    msg = conn.execute(
        "SELECT id FROM social_group_chat_messages WHERE id=? AND group_id=?", (message_id, group_id)
    ).fetchone()
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    existing = conn.execute(
        "SELECT 1 FROM social_group_chat_reactions WHERE message_id=? AND profile_id=? AND emoji=?",
        (message_id, profile_id, emoji)
    ).fetchone()
    if existing:
        conn.execute(
            "DELETE FROM social_group_chat_reactions WHERE message_id=? AND profile_id=? AND emoji=?",
            (message_id, profile_id, emoji)
        )
        active = False
    else:
        conn.execute(
            "INSERT INTO social_group_chat_reactions (message_id, group_id, profile_id, emoji, created_at) VALUES (?,?,?,?,?)",
            (message_id, group_id, profile_id, emoji, _now())
        )
        active = True
    conn.commit()
    rows = conn.execute(
        "SELECT emoji, COUNT(*) AS cnt FROM social_group_chat_reactions WHERE message_id=? GROUP BY emoji",
        (message_id,)
    ).fetchall()
    return JSONResponse({"ok": True, "active": active,
                         "reactions": [{"emoji": r["emoji"], "count": r["cnt"]} for r in rows]})


@router.put("/groups/{group_id}/chat/messages/{message_id}")
def edit_group_chat_message(group_id: str, message_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Текст обязателен")
    conn = _get_db()
    msg = conn.execute(
        "SELECT * FROM social_group_chat_messages WHERE id=? AND group_id=?", (message_id, group_id)
    ).fetchone()
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    if msg["sender_id"] != profile_id:
        membership = conn.execute(
            "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
        ).fetchone()
        if not membership or membership["role"] not in _GROUP_MOD_ROLES:
            raise HTTPException(403, "Нет прав на редактирование")
    conn.execute(
        "UPDATE social_group_chat_messages SET content=?, edited_at=? WHERE id=?",
        (content, _now(), message_id)
    )
    conn.commit()
    return JSONResponse({"ok": True})


@router.delete("/groups/{group_id}/chat/messages/{message_id}")
def delete_group_chat_message(group_id: str, message_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    msg = conn.execute(
        "SELECT * FROM social_group_chat_messages WHERE id=? AND group_id=?", (message_id, group_id)
    ).fetchone()
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    if msg["sender_id"] != profile_id:
        membership = conn.execute(
            "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
        ).fetchone()
        if not membership or membership["role"] not in _GROUP_MOD_ROLES:
            raise HTTPException(403, "Нет прав на удаление")
    conn.execute("DELETE FROM social_group_chat_messages WHERE id=?", (message_id,))
    conn.execute("DELETE FROM social_group_chat_reactions WHERE message_id=?", (message_id,))
    conn.commit()
    return JSONResponse({"ok": True})


@router.post("/groups/{group_id}/chat/messages/{message_id}/pin")
def pin_group_chat_message(group_id: str, message_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)
    ).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Закреплять могут только администраторы")
    msg = conn.execute(
        "SELECT id FROM social_group_chat_messages WHERE id=? AND group_id=?", (message_id, group_id)
    ).fetchone()
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    pinned = bool(data.get("pinned", True))
    conn.execute("UPDATE social_group_chat_messages SET is_pinned=? WHERE id=?", (1 if pinned else 0, message_id))
    conn.commit()
    return JSONResponse({"ok": True, "pinned": pinned})

@router.post("/groups/{group_id}/chat")
async def send_group_chat(group_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    content = (data.get("content") or "").strip()
    attachments = data.get("attachments", "[]")
    # Как и в create_group_post: проверять надо разобранные вложения, а не
    # строку "[]", которая истинна и пропускала бы пустое сообщение.
    attachments_json = _safe_attachments(attachments)
    if not content and attachments_json == "[]":
        raise HTTPException(400, "Текст обязателен")
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership:
        raise HTTPException(403, "Вы не участник")
    reply_to = (data.get("reply_to") or "").strip()
    if reply_to:
        exists = conn.execute(
            "SELECT 1 FROM social_group_chat_messages WHERE id=? AND group_id=?", (reply_to, group_id)
        ).fetchone()
        if not exists:
            reply_to = ""
    msg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO social_group_chat_messages (id, group_id, sender_id, content, created_at, attachments, reply_to) VALUES (?,?,?,?,?,?,?)",
        (msg_id, group_id, profile_id, content, now, attachments_json, reply_to)
    )
    conn.execute("UPDATE social_groups SET last_activity=? WHERE id=?", (now, group_id))
    conn.commit()
    # WebSocket notification
    try:
        await manager.broadcast_to_group(group_id, {
            "type": "group_chat",
            "group_id": group_id,
            "sender_id": profile_id,
            "content": content[:100],
        }, exclude_profile_id=profile_id)
    except Exception:
        swallow.note('social_module.send_group_chat:L5371')
        pass
    except Exception:
        swallow.note('social_module.send_group_chat:L5374')
        pass
    _notify_bots("group", group_id, profile_id, content, msg_id)
    _notify_ai("group", group_id, profile_id, content, msg_id)
    return JSONResponse({"id": msg_id, "ok": True})

# --- Activity ---

@router.get("/groups/{group_id}/activity")
def get_group_activity(group_id: str, request: Request, limit: int = Query(20)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership:
        raise HTTPException(403, "Вы не участник")
    # Combine posts and member joins
    posts = conn.execute(
        "SELECT 'post' as type, p.id, p.content, p.created_at, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_posts p JOIN social_users u ON p.author_id=u.profile_id WHERE p.group_id=? ORDER BY p.created_at DESC LIMIT ?",
        (group_id, limit)
    ).fetchall()
    joins = conn.execute(
        "SELECT 'join' as type, gm.profile_id as id, '' as content, gm.joined_at as created_at, u.display_name, u.avatar_path, u.avatar_crop FROM social_group_members gm JOIN social_users u ON gm.profile_id=u.profile_id WHERE gm.group_id=? ORDER BY gm.joined_at DESC LIMIT ?",
        (group_id, limit)
    ).fetchall()
    combined = []
    for r in posts:
        combined.append({"type":"post","text":f"опубликовал(а) пост","content":r["content"],"time":r["created_at"],"user":r["display_name"] or r["author_id"],"avatar":r["avatar_path"]})
    for r in joins:
        combined.append({"type":"join","text":"присоединился(ась) к группе","content":"","time":r["created_at"],"user":r["display_name"] or r["profile_id"],"avatar":r["avatar_path"]})
    combined.sort(key=lambda x: x["time"], reverse=True)
    return JSONResponse(combined[:limit])

# --- Upload post media ---

@router.post("/upload/post-media")
def upload_post_media(request: Request, file: UploadFile = File(...)):
    # Синхронная обёртка принципиальна: внутри только блокирующий дисковый
    # I/O, а async-версия выполняла бы её прямо на event loop — на время
    # записи фильма в 100 МБ останавливался бы весь сервер, включая
    # ленты и WebSocket. FastAPI пуляет такой обработчик в threadpool,
    # поэтому несколько загрузок идут параллельно и не мешают остальным.
    profile_id = _get_active_profile(request)
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT.union({'.mp4', '.webm', '.mov', '.avi', '.mp3', '.m4a', '.ogg', '.wav', '.flac'}):
        raise HTTPException(400, "Недопустимый формат")
    filename = f"{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(POST_MEDIA_DIR, filename)
    _save_upload(file, filepath, _MAX_POST_MEDIA_BYTES, image_only=ext in _ALLOWED_IMAGE_EXT)
    return JSONResponse({"ok": True, "path": f"/api/social/file/post_media/{filename}"})

# --- Group uploads ---

@router.post("/groups/{group_id}/upload/avatar")
def upload_group_avatar(group_id: str, request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат")
    filename = f"{group_id}_avatar{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    conn.execute("UPDATE social_groups SET avatar_path=? WHERE id=?", (f"/api/social/file/root/{filename}", group_id))
    conn.commit()
    return JSONResponse({"ok": True, "path": f"/api/social/file/root/{filename}"})

@router.post("/groups/{group_id}/upload/cover")
def upload_group_cover(group_id: str, request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    membership = conn.execute("SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?", (group_id, profile_id)).fetchone()
    if not membership or membership["role"] not in _GROUP_MOD_ROLES:
        raise HTTPException(403, "Нет прав")
    ext = os.path.splitext(file.filename or ".jpg")[1].lower()
    if ext not in _ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Недопустимый формат")
    filename = f"{group_id}_cover{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    _save_upload(file, filepath, _MAX_IMAGE_BYTES, image_only=True)
    conn.execute("UPDATE social_groups SET cover_path=? WHERE id=?", (f"/api/social/file/root/{filename}", group_id))
    conn.commit()
    return JSONResponse({"ok": True, "path": f"/api/social/file/root/{filename}"})

# --- Group comment API ---

def _group_post_comment_row(row, viewer, role):
    cd = _row_to_dict(row)
    cd["display_name"] = cd.get("display_name") or cd.get("author_id")
    cd["can_delete"] = cd["author_id"] == viewer or role in ("owner", "admin", "moderator")
    cd["can_edit"] = cd["author_id"] == viewer
    return cd


def _group_post_comment_permission(conn, group_id, post_id, viewer):
    """Права на комментарии поста группы: (post, роль).

    Проверка членства обязательна. Раньше её не было ни на чтении, ни на
    записи, поэтому комментарии к посту закрытой группы читались и
    дописывались любым, кто знал post_id.
    """
    post = conn.execute(
        "SELECT * FROM social_group_posts WHERE id=? AND group_id=?", (post_id, group_id)
    ).fetchone()
    if not post:
        raise HTTPException(404, "Пост не найден")
    member = conn.execute(
        "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
        (group_id, viewer)).fetchone()
    if not member:
        raise HTTPException(403, "Вы не участник группы")
    return post, member["role"]


@router.get("/groups/{group_id}/posts/{post_id}/comments")
def get_group_post_comments(group_id: str, post_id: str, request: Request):
    viewer = _get_active_profile(request)
    conn = _get_db()
    _, role = _group_post_comment_permission(conn, group_id, post_id, viewer)
    rows = conn.execute(
        "SELECT c.*, COALESCE(u.display_name, c.author_id) AS display_name, u.avatar_path, u.avatar_crop "
        "FROM social_comments c LEFT JOIN social_users u ON c.author_id=u.profile_id "
        "WHERE c.target_type='group_post' AND c.target_id=? ORDER BY c.created_at ASC",
        (post_id,)
    ).fetchall()
    return JSONResponse([_group_post_comment_row(r, viewer, role) for r in rows])

@router.post("/groups/{group_id}/posts/{post_id}/comments")
def add_group_post_comment(group_id: str, post_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Текст обязателен")
    _check_rate_limit(f"gcomment:{profile_id}", 20, 60.0)
    conn = _get_db()
    _group_post_comment_permission(conn, group_id, post_id, profile_id)
    parent_id = (data.get("parent_id") or "").strip() or None
    if parent_id:
        ok = conn.execute(
            "SELECT 1 FROM social_comments WHERE id=? AND target_type='group_post' AND target_id=?",
            (parent_id, post_id)).fetchone()
        if not ok:
            parent_id = None
    _ensure_user(profile_id)
    comment_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_comments (id, post_id, author_id, content, created_at, target_type, target_id, parent_id) "
        "VALUES (?,NULL,?,?,?,'group_post',?,?)",
        (comment_id, profile_id, content[:4000], _now(), post_id, parent_id)
    )
    _parse_mentions(content[:4000], comment_id, profile_id, conn, "group_post", post_id)
    conn.commit()
    row = conn.execute(
        "SELECT c.*, COALESCE(u.display_name, c.author_id) AS display_name, u.avatar_path, u.avatar_crop "
        "FROM social_comments c LEFT JOIN social_users u ON c.author_id=u.profile_id WHERE c.id=?",
        (comment_id,)
    ).fetchone()
    return JSONResponse({"id": comment_id, "ok": True,
                         "comment": _group_post_comment_row(row, profile_id, "member")})

@router.put("/groups/{group_id}/posts/{post_id}/comments/{comment_id}")
def update_group_post_comment(group_id: str, post_id: str, comment_id: str,
                                    request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Текст обязателен")
    conn = _get_db()
    _group_post_comment_permission(conn, group_id, post_id, profile_id)
    row = conn.execute(
        "SELECT author_id FROM social_comments WHERE id=? AND target_type='group_post' AND target_id=?",
        (comment_id, post_id)).fetchone()
    if not row:
        raise HTTPException(404, "Комментарий не найден")
    if row["author_id"] != profile_id:
        raise HTTPException(403, "Можно править только свой комментарий")
    conn.execute(
        "UPDATE social_comments SET content=?, updated_at=? WHERE id=?",
        (content[:4000], _now(), comment_id))
    conn.commit()
    return JSONResponse({"ok": True})

@router.delete("/groups/{group_id}/posts/{post_id}/comments/{comment_id}")
def delete_group_post_comment(group_id: str, post_id: str, comment_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    _, role = _group_post_comment_permission(conn, group_id, post_id, profile_id)
    row = conn.execute(
        "SELECT author_id FROM social_comments WHERE id=? AND target_type='group_post' AND target_id=?",
        (comment_id, post_id)).fetchone()
    if not row:
        raise HTTPException(404, "Комментарий не найден")
    if row["author_id"] != profile_id and role not in ("owner", "admin", "moderator"):
        raise HTTPException(403, "Нельзя удалить чужой комментарий")
    conn.execute("DELETE FROM social_comments WHERE id=?", (comment_id,))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Repost API ---

@router.post("/repost/{post_id}")
def repost_post(post_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    original = conn.execute("SELECT * FROM social_posts WHERE id=?", (post_id,)).fetchone()
    if not original:
        raise HTTPException(404, "Пост не найден")
    new_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO social_posts (id, author_id, content, created_at, privacy, attachments, repost_id) VALUES (?,?,?,?,?,?,?)",
        (new_id, profile_id, original["content"], now, original["privacy"], original["attachments"], post_id)
    )
    conn.execute(
        "INSERT INTO social_feeds (id, user_id, post_id, author_id, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), profile_id, new_id, profile_id, now)
    )
    conn.commit()
    try:
        import activitypub_module
        activitypub_module.federate_announce(profile_id, post_id)
    except Exception:
        swallow.note('social_module.repost_post:L5598')
        pass
    return JSONResponse({"id": new_id, "ok": True})

@router.get("/share/groups")
def share_groups(request: Request, q: str = Query("")):
    """Куда можно поделиться: группы и каналы, где пользователь состоит.

    Список для окна «Поделиться». Раньше выбора места не было вовсе:
    кнопка на профиле лишь копировала ссылку в буфер.
    """
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT g.id, g.name, g.avatar_path, g.is_channel, m.role "
        "FROM social_groups g JOIN social_group_members m ON m.group_id = g.id "
        "WHERE m.profile_id = ? ORDER BY g.is_channel, g.name",
        (profile_id,)).fetchall()
    items = []
    for r in rows:
        if q and q.lower() not in (r["name"] or "").lower():
            continue
        items.append({
            "id": r["id"],
            "name": r["name"],
            "avatar_path": r["avatar_path"] or "",
            "is_channel": bool(r["is_channel"]),
            "role": r["role"],
        })
    return JSONResponse({"targets": items})

@router.post("/share")
async def share_item(request: Request, data: dict = Body(...)):
    """Поделиться фото, постом ленты или постом группы.

    Тело: { id, target, comment }
      target = "feed"            — в свою ленту
              "group:<group_id>" — на стену группы или канала
    Источник определяется по таблице, где лежит идентификатор, поэтому
    клиенту не нужно знать тип заранее.
    """
    profile_id = _get_active_profile(request)
    conn = _get_db()
    item_id = str(data.get("id") or "").strip()
    target = str(data.get("target") or "feed").strip()
    comment = str(data.get("comment") or "").strip()[:4000]
    if not item_id:
        raise HTTPException(400, "Нечего распространять")
    src = _load_shareable(conn, item_id)
    if not src:
        raise HTTPException(404, "Объект не найден")

    now = _now()
    new_id = str(uuid.uuid4())

    if target.startswith("group:"):
        group_id = target.split(":", 1)[1]
        # Проверки членства и прав на публикацию те же, что у create_group_post.
        group = conn.execute("SELECT * FROM social_groups WHERE id=?", (group_id,)).fetchone()
        if not group:
            raise HTTPException(404, "Группа не найдена")
        member = conn.execute(
            "SELECT role FROM social_group_members WHERE group_id=? AND profile_id=?",
            (group_id, profile_id)).fetchone()
        if not member:
            raise HTTPException(403, "Вы не участник группы")
        if group["is_channel"] and member["role"] != "admin":
            raise HTTPException(403, "В канал может писать только администратор")
        perm = group["post_permission"] or 0
        if perm == 2 and member["role"] != "admin":
            raise HTTPException(403, "Публиковать может только администратор")
        if perm == 1 and member["role"] not in _GROUP_MOD_ROLES:
            raise HTTPException(403, "Публиковать могут модераторы")
        # Вложения источника переиспользуются как есть, но ещё раз
        # проходят проверку: ссылка могла прийти из старой записи,
        # сделанной до того как _safe_attachments стал ограничивать пути.
        attachments = src.get("attachments") or "[]"
        if src.get("kind") == "photo":
            attachments = json.dumps([{"type": "image", "url": src.get("attachments"),
                                       "title": src.get("content") or ""}], ensure_ascii=False)
        elif not isinstance(attachments, str):
            attachments = json.dumps(attachments, ensure_ascii=False)
        attachments = _safe_attachments(attachments)
        conn.execute(
            "INSERT INTO social_group_posts (id, group_id, author_id, content, created_at, attachments, repost_id, repost_group_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (new_id, group_id, profile_id, comment or (src.get("content") or "")[:400],
             now, attachments, item_id, src.get("group_id") or ""))
        conn.execute("UPDATE social_groups SET last_activity=? WHERE id=?", (now, group_id))
        _parse_mentions(comment, new_id, profile_id, conn)
        for m in conn.execute(
                "SELECT profile_id FROM social_group_members WHERE group_id=? AND profile_id!=?",
                (group_id, profile_id)).fetchall():
            _create_notification(conn, m["profile_id"], "group_post", profile_id, "group", group_id)
        conn.commit()
        try:
            await manager.broadcast_to_group(group_id, {
                "type": "group_post", "group_id": group_id, "post_id": new_id,
                "author_id": profile_id, "content": (comment or "")[:120],
            }, exclude_profile_id=profile_id)
        except Exception:
            swallow.note('social_module.share_item:L5699')
            pass
        return JSONResponse({"ok": True, "post_id": new_id, "target": target})

    # В ленту. Фото превращается в пост с вложением, иначе картинка
    # потерялась бы: у social_posts нет отдельного поля для фото.
    content = comment
    attachments = "[]"
    repost_id = item_id
    if src.get("kind") == "photo":
        attachments = json.dumps([{"type": "image", "url": src.get("attachments"),
                                   "title": src.get("content") or ""}], ensure_ascii=False)
        repost_id = None
    else:
        content = comment or (src.get("content") or "")[:4000]
    if not content and attachments == "[]":
        raise HTTPException(400, "Текст обязателен")
    attachments = _safe_attachments(attachments)
    conn.execute(
        "INSERT INTO social_posts (id, author_id, content, created_at, privacy, attachments, repost_id) "
        "VALUES (?,?,?,?,0,?,?)",
        (new_id, profile_id, content, now, attachments, repost_id))
    conn.execute(
        "INSERT INTO social_feeds (id, user_id, post_id, author_id, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), profile_id, new_id, profile_id, now))
    _parse_mentions(content, new_id, profile_id, conn)
    conn.commit()
    return JSONResponse({"ok": True, "post_id": new_id, "target": "feed"})

# --- Hashtag API ---

@router.get("/hashtag/{tag}")
def search_hashtag(tag: str, request: Request, offset: int = Query(0), limit: int = Query(50)):
    conn = _get_db()
    rows = conn.execute(
        "SELECT p.*, u.display_name, u.avatar_path, u.avatar_crop FROM social_posts p "
        "JOIN social_hashtags h ON h.post_id = p.id "
        "JOIN social_users u ON p.author_id = u.profile_id "
        "WHERE h.tag = ? ORDER BY h.created_at DESC LIMIT ? OFFSET ?",
        (tag.lower(), limit, offset)
    ).fetchall()
    posts = []
    for r in rows:
        post = _row_to_dict(r)
        post["liked_by_me"] = False
        profile_id = _get_active_profile(request)
        liked = conn.execute(
            "SELECT 1 FROM social_likes WHERE user_id=? AND target_type='post' AND target_id=?",
            (profile_id, r["id"])
        ).fetchone()
        if liked:
            post["liked_by_me"] = True
        posts.append(post)
    return JSONResponse(posts)

@router.get("/hashtags/trending")
def trending_hashtags():
    conn = _get_db()
    rows = conn.execute(
        "SELECT tag, COUNT(*) as cnt FROM social_hashtags "
        "WHERE created_at > ? GROUP BY tag ORDER BY cnt DESC LIMIT 20",
        (time.time() - 86400 * 7,)
    ).fetchall()
    if rows:
        return JSONResponse([{"tag": r["tag"], "count": r["cnt"]} for r in rows])
    # Fallback to content-based extraction
    import re
    posts = conn.execute("SELECT content FROM social_posts WHERE created_at > ? ORDER BY created_at DESC LIMIT 200", (time.time() - 86400 * 7,)).fetchall()
    tag_counts = {}
    for r in posts:
        for t in re.findall(r'#(\w+)', r["content"]):
            tag_counts[t.lower()] = tag_counts.get(t.lower(), 0) + 1
    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    return JSONResponse([{"tag": t, "count": c} for t, c in sorted_tags[:20]])

# --- AI Recommendations API ---

@router.get("/recommendations")
def get_recommendations(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    # Find friends of friends
    friends = conn.execute(
        "SELECT CASE WHEN requester_id=? THEN addressee_id ELSE requester_id END AS fid FROM social_friendships WHERE (requester_id=? OR addressee_id=?) AND status='accepted'",
        (profile_id, profile_id, profile_id)
    ).fetchall()
    friend_ids = [f["fid"] for f in friends]
    if not friend_ids:
        # Just show recent users
        rows = conn.execute(
            "SELECT profile_id, display_name, avatar_path, avatar_crop FROM social_users WHERE profile_id != ? ORDER BY last_seen DESC LIMIT 5",
            (profile_id,)
        ).fetchall()
    result = []
    for r in rows:
        ud = _row_to_dict(r)
        if ud.get("last_seen"):
            ud["is_online"] = _is_online_from_last_seen(ud.get("last_seen"))
        else:
            ud["is_online"] = bool(ud.get("is_online"))
        result.append(ud)
    return JSONResponse(result)

# --- P2P Signal API ---

@router.post("/p2p/offer")
async def p2p_offer(request: Request, data: dict = Body(...)):
    from_id = _get_active_profile(request)
    to_id = data.get("to")
    signal = data.get("signal")
    conv_id = data.get("conversation_id", "")
    if not to_id or not signal:
        raise HTTPException(400, "Missing params")
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_p2p_signals (id, from_id, to_id, conversation_id, signal, created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), from_id, to_id, conv_id, signal, _now())
    )
    conn.commit()
    return JSONResponse({"ok": True})

@router.post("/p2p/answer")
async def p2p_answer(request: Request, data: dict = Body(...)):
    return await p2p_offer(request, data)

@router.get("/p2p/pending")
def get_pending_signals(request: Request, conv_id: Optional[str] = Query(None)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if conv_id:
        rows = conn.execute(
            "SELECT * FROM social_p2p_signals WHERE to_id=? AND is_consumed=0 AND conversation_id=? ORDER BY created_at ASC",
            (profile_id, conv_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM social_p2p_signals WHERE to_id=? AND is_consumed=0 ORDER BY created_at ASC",
            (profile_id,)
        ).fetchall()
    conn.execute("UPDATE social_p2p_signals SET is_consumed=1 WHERE to_id=?", (profile_id,))
    conn.commit()
    return JSONResponse([_row_to_dict(r) for r in rows])

# --- Servers (Discord-like rooms) API ---

_ROLE_LEVEL = {"owner": 3, "admin": 2, "mod": 1, "member": 0}

def _server_role(conn, server_id, profile_id):
    row = conn.execute(
        "SELECT role FROM social_server_members WHERE server_id=? AND profile_id=?",
        (server_id, profile_id)
    ).fetchone()
    return row["role"] if row else None

def _channel_access_ok(channel, profile_id):
    if not channel or not channel["is_private"]:
        return True
    try:
        access = json.loads(channel["access_json"] or "[]")
    except Exception:
        swallow.note('social_module._channel_access_ok:L5859')
        access = []
    return profile_id in access

def _gen_invite_code():
    import random, string
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(8))

def _channel_row(channel, profile_id):
    ch = _row_to_dict(channel)
    return {
        "id": ch["id"], "server_id": ch["server_id"], "name": ch["name"],
        "type": ch["type"], "is_private": ch["is_private"], "created_at": ch["created_at"]
    }

def _server_summary(row, conn, profile_id):
    s = _row_to_dict(row)
    cnt = conn.execute("SELECT COUNT(*) c FROM social_server_members WHERE server_id=?", (s["id"],)).fetchone()["c"]
    role = _server_role(conn, s["id"], profile_id)
    return {
        "id": s["id"], "name": s["name"], "icon_path": s["icon_path"],
        "owner_id": s["owner_id"], "invite_code": s["invite_code"],
        "is_private": s["is_private"], "member_count": cnt, "role": role,
        "created_at": s["created_at"]
    }

@router.post("/servers")
def create_server(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Укажите название сервера")
    if len(name) > 40:
        raise HTTPException(400, "Название слишком длинное")
    conn = _get_db()
    server_id = str(uuid.uuid4())
    code = _gen_invite_code()
    conn.execute(
        "INSERT INTO social_servers (id, name, icon_path, owner_id, invite_code, is_private, created_at) VALUES (?,?,?,?,?,?,?)",
        (server_id, name, data.get("icon_path", ""), profile_id, code, 1 if data.get("is_private") else 0, _now())
    )
    conn.execute(
        "INSERT INTO social_server_members (server_id, profile_id, role, joined_at) VALUES (?,?,?,?)",
        (server_id, profile_id, "owner", _now())
    )
    conn.execute(
        "INSERT INTO social_server_channels (id, server_id, name, type, is_private, access_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), server_id, "общий", "text", 0, "[]", _now())
    )
    conn.commit()
    return JSONResponse({"id": server_id, "ok": True})

@router.get("/servers")
def list_servers(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT s.* FROM social_servers s JOIN social_server_members m ON m.server_id=s.id "
        "WHERE m.profile_id=? ORDER BY s.created_at", (profile_id,)
    ).fetchall()
    return JSONResponse([_server_summary(r, conn, profile_id) for r in rows])

@router.get("/servers/{server_id}")
def get_server(server_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    s = conn.execute("SELECT * FROM social_servers WHERE id=?", (server_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Сервер не найден")
    if not _server_role(conn, server_id, profile_id):
        raise HTTPException(403, "Вы не участник этого сервера")
    channels = []
    for ch in conn.execute(
        "SELECT * FROM social_server_channels WHERE server_id=? ORDER BY type DESC, created_at", (server_id,)
    ).fetchall():
        if _channel_access_ok(ch, profile_id):
            channels.append(_channel_row(ch, profile_id))
    members = []
    for m in conn.execute(
        "SELECT sm.profile_id, sm.role, u.display_name, u.avatar_path FROM social_server_members sm "
        "LEFT JOIN social_users u ON u.profile_id=sm.profile_id WHERE sm.server_id=? ORDER BY sm.joined_at",
        (server_id,)
    ).fetchall():
        members.append({"profile_id": m["profile_id"], "role": m["role"],
                        "display_name": m["display_name"] or m["profile_id"], "avatar_path": m["avatar_path"] or ""})
    presence = {}
    for ch in channels:
        pc = manager.voice_presence.get(ch["id"])
        if pc:
            presence[ch["id"]] = [{"profile_id": pid, **st} for pid, st in pc.items()]
    return JSONResponse({
        **_server_summary(s, conn, profile_id),
        "channels": channels,
        "members": members,
        "presence": presence
    })

@router.post("/servers/join")
def join_server(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    code = (data.get("code") or "").strip().upper()
    conn = _get_db()
    s = conn.execute("SELECT * FROM social_servers WHERE invite_code=?", (code,)).fetchone()
    if not s:
        raise HTTPException(404, "Неверный код приглашения")
    if _server_role(conn, s["id"], profile_id):
        return JSONResponse({"id": s["id"], "already": True})
    conn.execute(
        "INSERT INTO social_server_members (server_id, profile_id, role, joined_at) VALUES (?,?,?,?)",
        (s["id"], profile_id, "member", _now())
    )
    conn.commit()
    return JSONResponse({"id": s["id"], "ok": True})

@router.post("/servers/{server_id}/leave")
async def leave_server(server_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    role = _server_role(conn, server_id, profile_id)
    if not role:
        raise HTTPException(403, "Вы не участник")
    if role == "owner":
        raise HTTPException(400, "Владелец не может покинуть сервер — удалите его")
    conn.execute("DELETE FROM social_server_members WHERE server_id=? AND profile_id=?", (server_id, profile_id))
    conn.commit()
    await manager.leave_all_voice(profile_id)
    return JSONResponse({"ok": True})

def _purge_channel_rows(conn, channel_ids):
    """Удалить всё содержимое каналов сервера (сообщения, реакции, просмотры,
    альбомы и статьи). Без этого удаление сервера/канала оставляло сирот."""
    channel_ids = [c for c in (channel_ids or []) if c]
    if not channel_ids:
        return
    marks = ",".join("?" * len(channel_ids))
    msg_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM social_server_messages WHERE channel_id IN ({marks})", channel_ids).fetchall()]
    if msg_ids:
        m2 = ",".join("?" * len(msg_ids))
        conn.execute(f"DELETE FROM social_server_reactions WHERE message_id IN ({m2})", msg_ids)
    conn.execute(f"DELETE FROM social_server_messages WHERE channel_id IN ({marks})", channel_ids)
    conn.execute(f"DELETE FROM social_channel_views WHERE channel_id IN ({marks})", channel_ids)
    conn.execute(
        f"DELETE FROM social_album_photos WHERE album_id IN "
        f"(SELECT id FROM social_albums WHERE channel_id IN ({marks}))", channel_ids)
    conn.execute(f"DELETE FROM social_albums WHERE channel_id IN ({marks})", channel_ids)
    conn.execute(
        f"DELETE FROM social_article_sections WHERE article_id IN "
        f"(SELECT id FROM social_articles WHERE channel_id IN ({marks}))", channel_ids)
    conn.execute(f"DELETE FROM social_articles WHERE channel_id IN ({marks})", channel_ids)


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    s = conn.execute("SELECT * FROM social_servers WHERE id=?", (server_id,)).fetchone()
    if not s:
        raise HTTPException(404, "Сервер не найден")
    if s["owner_id"] != profile_id:
        raise HTTPException(403, "Только владелец может удалить сервер")
    channel_ids = [ch["id"] for ch in conn.execute(
        "SELECT id FROM social_server_channels WHERE server_id=?", (server_id,)).fetchall()]
    _purge_channel_rows(conn, channel_ids)
    conn.execute("DELETE FROM social_server_channels WHERE server_id=?", (server_id,))
    conn.execute("DELETE FROM social_server_members WHERE server_id=?", (server_id,))
    conn.execute("DELETE FROM social_servers WHERE id=?", (server_id,))
    conn.commit()
    await manager.leave_all_voice(profile_id)
    return JSONResponse({"ok": True})

_ROLES = {"admin", "mod"}

@router.put("/servers/{server_id}/members/{member_id}/role")
async def set_member_role(server_id: str, member_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    role = (data.get("role") or "").lower()
    if role not in _ROLES and role != "member":
        raise HTTPException(400, "Недопустимая роль")
    conn = _get_db()
    my_role = _server_role(conn, server_id, profile_id)
    if my_role != "owner":
        raise HTTPException(403, "Только владелец управляет ролями")
    if member_id == profile_id:
        raise HTTPException(400, "Нельзя изменить свою роль")
    if not _server_role(conn, server_id, member_id):
        raise HTTPException(404, "Участник не найден")
    conn.execute("UPDATE social_server_members SET role=? WHERE server_id=? AND profile_id=?",
                 ("member" if role == "member" else role, server_id, member_id))
    conn.commit()
    await manager.broadcast_to_server(server_id, {"type": "room_update", "server_id": server_id,
                                                  "what": "role", "profile_id": member_id, "role": role})
    return JSONResponse({"ok": True})

@router.delete("/servers/{server_id}/members/{member_id}")
async def server_kick_member(server_id: str, member_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    my_role = _server_role(conn, server_id, profile_id)
    target_role = _server_role(conn, server_id, member_id)
    if not my_role or my_role not in ("owner", "admin", "mod"):
        raise HTTPException(403, "Нет прав")
    if not target_role:
        raise HTTPException(404, "Участник не найден")
    if target_role == "owner":
        raise HTTPException(400, "Нельзя исключить владельца")
    if my_role == "mod" and target_role in ("admin", "mod"):
        raise HTTPException(403, "Модератор не может исключить модератора/администратора")
    if my_role == "admin" and target_role == "admin":
        raise HTTPException(403, "Администратор не может исключить администратора")
    conn.execute("DELETE FROM social_server_members WHERE server_id=? AND profile_id=?", (server_id, member_id))
    conn.commit()
    await manager.leave_all_voice(member_id, server_id)
    await manager.broadcast_to_server(server_id, {"type": "room_update", "server_id": server_id,
                                                  "what": "kick", "profile_id": member_id})
    return JSONResponse({"ok": True})

@router.post("/servers/{server_id}/channels")
async def create_channel(server_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Укажите название канала")
    if len(name) > 30:
        raise HTTPException(400, "Слишком длинное название")
    ctype = data.get("type", "text")
    if ctype not in ("text", "voice"):
        raise HTTPException(400, "Недопустимый тип")
    conn = _get_db()
    role = _server_role(conn, server_id, profile_id)
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Только владелец или администратор")
    if not conn.execute("SELECT id FROM social_servers WHERE id=?", (server_id,)).fetchone():
        raise HTTPException(404, "Сервер не найден")
    ch_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO social_server_channels (id, server_id, name, type, is_private, access_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (ch_id, server_id, name, ctype, 0, "[]", _now())
    )
    conn.commit()
    await manager.broadcast_to_server(server_id, {"type": "room_update", "server_id": server_id,
                                                  "what": "channel", "channel": _channel_row(
                                                      conn.execute("SELECT * FROM social_server_channels WHERE id=?", (ch_id,)).fetchone(), profile_id)})
    return JSONResponse({"id": ch_id, "ok": True})

@router.patch("/servers/{server_id}/channels/{channel_id}")
async def update_channel(server_id: str, channel_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    role = _server_role(conn, server_id, profile_id)
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Только владелец или администратор")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=? AND server_id=?", (channel_id, server_id)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    name = (data.get("name") or ch["name"]).strip()[:30]
    is_private = data.get("is_private", ch["is_private"])
    access = data.get("access")
    if access is not None:
        access_json = json.dumps(access, ensure_ascii=False)
    else:
        access_json = ch["access_json"]
    conn.execute(
        "UPDATE social_server_channels SET name=?, is_private=?, access_json=? WHERE id=?",
        (name, 1 if is_private else 0, access_json, channel_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM social_server_channels WHERE id=?", (channel_id,)).fetchone()
    await manager.broadcast_to_server(server_id, {"type": "room_update", "server_id": server_id,
                                                  "what": "channel", "channel": _channel_row(row, profile_id)})
    return JSONResponse({"ok": True})

@router.delete("/servers/{server_id}/channels/{channel_id}")
async def delete_channel(server_id: str, channel_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    role = _server_role(conn, server_id, profile_id)
    if role not in ("owner", "admin"):
        raise HTTPException(403, "Только владелец или администратор")
    if not conn.execute("SELECT id FROM social_server_channels WHERE id=? AND server_id=?", (channel_id, server_id)).fetchone():
        raise HTTPException(404, "Канал не найден")
    _purge_channel_rows(conn, [channel_id])
    conn.execute("DELETE FROM social_server_channels WHERE id=?", (channel_id,))
    conn.commit()
    await manager.broadcast_to_server(server_id, {"type": "room_update", "server_id": server_id,
                                                  "what": "channel_del", "channel_id": channel_id})
    return JSONResponse({"ok": True})

@router.get("/servers/{server_id}/channels/{channel_id}/messages")
def get_server_messages(server_id: str, channel_id: str, request: Request,
                              before: Optional[float] = Query(None), limit: int = Query(40)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    if not _server_role(conn, server_id, profile_id):
        raise HTTPException(403, "Вы не участник")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=? AND server_id=?", (channel_id, server_id)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    if not _channel_access_ok(ch, profile_id):
        raise HTTPException(403, "Нет доступа к приватному каналу")
    limit = max(1, min(100, limit))
    if before:
        rows = conn.execute(
            "SELECT * FROM social_server_messages WHERE channel_id=? AND created_at<? ORDER BY created_at DESC LIMIT ?",
            (channel_id, before, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM social_server_messages WHERE channel_id=? ORDER BY created_at DESC LIMIT ?",
            (channel_id, limit)
        ).fetchall()
    items = [_row_to_dict(r) for r in rows]
    if items:
        ids = [m["id"] for m in items]
        ph = ",".join("?" * len(ids))
        reacts = conn.execute(
            f"SELECT message_id, emoji, COUNT(*) AS cnt, SUM(CASE WHEN profile_id=? THEN 1 ELSE 0 END) AS mine "
            f"FROM social_server_reactions WHERE message_id IN ({ph}) GROUP BY message_id, emoji",
            (profile_id, *ids)
        ).fetchall()
        by_msg = {}
        for r in reacts:
            by_msg.setdefault(r["message_id"], []).append({"emoji": r["emoji"], "count": r["cnt"], "mine": bool(r["mine"])})
        for m in items:
            m["reactions"] = by_msg.get(m["id"], [])
        # Флаг is_bot: без него сообщение бота в канале неотличимо от
        # сообщения человека, хотя в social_users он есть.
        sids = list({m["sender_id"] for m in items})
        sph = ",".join("?" * len(sids))
        bot_rows = conn.execute(
            f"SELECT profile_id FROM social_users WHERE is_bot=1 AND profile_id IN ({sph})",
            sids).fetchall()
        bots = {r["profile_id"] for r in bot_rows}
        for m in items:
            m["is_bot"] = m["sender_id"] in bots
    return JSONResponse(items)

@router.post("/servers/{server_id}/channels/{channel_id}/messages")
async def send_server_message(server_id: str, channel_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    if not _check_rate_limit(f"srvmsg:{profile_id}", 30, 60):
        raise HTTPException(429, "Слишком много сообщений.")
    conn = _get_db()
    if not _server_role(conn, server_id, profile_id):
        raise HTTPException(403, "Вы не участник")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=? AND server_id=?", (channel_id, server_id)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    if not _channel_access_ok(ch, profile_id):
        raise HTTPException(403, "Нет доступа к приватному каналу")
    content = (data.get("content") or "").strip()
    sticker = (data.get("sticker") or "").strip()
    attachment = (data.get("attachment") or "").strip()
    reply_to = (data.get("reply_to") or "").strip()
    if not content and not sticker and not attachment:
        raise HTTPException(400, "Пустое сообщение")
    if len(content) > 4000:
        raise HTTPException(400, "Сообщение слишком длинное")
    if reply_to:
        rp = conn.execute("SELECT id FROM social_server_messages WHERE id=?", (reply_to,)).fetchone()
        if not rp:
            reply_to = ""
    msg_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO social_server_messages (id, channel_id, sender_id, content, sticker, attachment, reply_to, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (msg_id, channel_id, profile_id, content, sticker, attachment, reply_to, now)
    )
    conn.commit()
    msg = {"id": msg_id, "channel_id": channel_id, "sender_id": profile_id, "content": content,
           "sticker": sticker, "attachment": attachment, "reply_to": reply_to,
           "edited_at": 0, "is_pinned": 0, "created_at": now}
    await manager.broadcast_to_server(server_id, {"type": "server_msg", "server_id": server_id, "channel_id": channel_id, "message": msg})
    try:
        from social_net_module import on_local_server_message
        on_local_server_message(server_id, channel_id, msg, profile_id)
    except Exception:
        swallow.note('social_module.send_server_message:L6237')
        pass
    # Только текст: ответ бота на фото или файл бессмыслен.
    if content:
        _notify_bots("server", server_id, profile_id, content, msg_id)
        _notify_ai("server", server_id, profile_id, content, msg_id, channel_id)
    return JSONResponse(msg)

@router.delete("/server_messages/{message_id}")
async def delete_server_message(message_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    m = conn.execute("SELECT * FROM social_server_messages WHERE id=?", (message_id,)).fetchone()
    if not m:
        raise HTTPException(404, "Сообщение не найдено")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=?", (m["channel_id"],)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    role = _server_role(conn, ch["server_id"], profile_id)
    if m["sender_id"] != profile_id and (not role or role not in ("owner", "admin", "mod")):
        raise HTTPException(403, "Нет прав на удаление")
    conn.execute("DELETE FROM social_server_messages WHERE id=?", (message_id,))
    conn.execute("DELETE FROM social_server_reactions WHERE message_id=?", (message_id,))
    conn.commit()
    await manager.broadcast_to_server(ch["server_id"], {"type": "room_update", "server_id": ch["server_id"],
                                                        "what": "msg_del", "message_id": message_id})
    return JSONResponse({"ok": True})

@router.patch("/server_messages/{message_id}")
async def edit_server_message(message_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    m = conn.execute("SELECT * FROM social_server_messages WHERE id=?", (message_id,)).fetchone()
    if not m:
        raise HTTPException(404, "Сообщение не найдено")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=?", (m["channel_id"],)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    role = _server_role(conn, ch["server_id"], profile_id)
    if m["sender_id"] != profile_id and (not role or role not in ("owner", "admin", "mod")):
        raise HTTPException(403, "Нет прав на редактирование")
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "Пустое сообщение")
    if len(content) > 4000:
        raise HTTPException(400, "Сообщение слишком длинное")
    now = _now()
    # Раньше обнулялся только sticker, а attachment оставался — сообщение
    # со стикером+вложением после правки превращалось в «кашу»: текст
    # новый, картинка старая. Стикер и вложение — одна сущность, гасим оба.
    conn.execute("UPDATE social_server_messages SET content=?, sticker='', attachment='', edited_at=? WHERE id=?",
                 (content, now, message_id))
    conn.commit()
    msg = _row_to_dict(conn.execute("SELECT * FROM social_server_messages WHERE id=?", (message_id,)).fetchone())
    reacts = conn.execute(
        "SELECT emoji, COUNT(*) AS cnt, SUM(CASE WHEN profile_id=? THEN 1 ELSE 0 END) AS mine "
        "FROM social_server_reactions WHERE message_id=? GROUP BY emoji", (profile_id, message_id)
    ).fetchall()
    msg["reactions"] = [{"emoji": r["emoji"], "count": r["cnt"], "mine": bool(r["mine"])} for r in reacts]
    await manager.broadcast_to_server(ch["server_id"], {"type": "room_update", "server_id": ch["server_id"],
                                                        "what": "msg_edit", "channel_id": m["channel_id"],
                                                        "message": msg})
    return JSONResponse({"ok": True, "message": msg})

@router.put("/server_messages/{message_id}/pin")
async def pin_server_message(message_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    m = conn.execute("SELECT * FROM social_server_messages WHERE id=?", (message_id,)).fetchone()
    if not m:
        raise HTTPException(404, "Сообщение не найдено")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=?", (m["channel_id"],)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    role = _server_role(conn, ch["server_id"], profile_id)
    if not role or role not in ("owner", "admin", "mod"):
        raise HTTPException(403, "Нет прав на закреп")
    pinned = 1 if data.get("pinned") else 0
    conn.execute("UPDATE social_server_messages SET is_pinned=? WHERE id=?", (pinned, message_id))
    conn.commit()
    await manager.broadcast_to_server(ch["server_id"], {"type": "room_update", "server_id": ch["server_id"],
                                                        "what": "pin", "channel_id": m["channel_id"],
                                                        "message_id": message_id, "is_pinned": pinned})
    return JSONResponse({"ok": True})

@router.put("/server_messages/{message_id}/reactions")
async def toggle_server_reaction(message_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    emoji = (data.get("emoji") or "").strip()
    if not emoji:
        raise HTTPException(400, "Нет эмодзи")
    conn = _get_db()
    m = conn.execute("SELECT * FROM social_server_messages WHERE id=?", (message_id,)).fetchone()
    if not m:
        raise HTTPException(404, "Сообщение не найдено")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=?", (m["channel_id"],)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    if not _server_role(conn, ch["server_id"], profile_id):
        raise HTTPException(403, "Вы не участник")
    existing = conn.execute("SELECT 1 FROM social_server_reactions WHERE message_id=? AND profile_id=? AND emoji=?",
                            (message_id, profile_id, emoji)).fetchone()
    if existing:
        conn.execute("DELETE FROM social_server_reactions WHERE message_id=? AND profile_id=? AND emoji=?",
                     (message_id, profile_id, emoji))
    else:
        conn.execute("INSERT INTO social_server_reactions (message_id, profile_id, emoji, created_at) VALUES (?,?,?,?)",
                     (message_id, profile_id, emoji, _now()))
    conn.commit()
    reacts = conn.execute(
        "SELECT emoji, COUNT(*) AS cnt, SUM(CASE WHEN profile_id=? THEN 1 ELSE 0 END) AS mine "
        "FROM social_server_reactions WHERE message_id=? GROUP BY emoji ORDER BY cnt DESC",
        (profile_id, message_id)
    ).fetchall()
    reactions = [{"emoji": r["emoji"], "count": r["cnt"], "mine": bool(r["mine"])} for r in reacts]
    await manager.broadcast_to_server(ch["server_id"], {"type": "room_update", "server_id": ch["server_id"],
                                                        "what": "react", "channel_id": m["channel_id"],
                                                        "message_id": message_id, "reactions": reactions})
    return JSONResponse({"ok": True, "reactions": reactions})

# --- Voice room presence ---

@router.post("/servers/{server_id}/channels/{channel_id}/join")
async def join_voice_channel(server_id: str, channel_id: str, request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    role = _server_role(conn, server_id, profile_id)
    if not role:
        raise HTTPException(403, "Вы не участник")
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=? AND server_id=?", (channel_id, server_id)).fetchone()
    if not ch:
        raise HTTPException(404, "Канал не найден")
    if ch["type"] != "voice":
        raise HTTPException(400, "Это не голосовой канал")
    if not _channel_access_ok(ch, profile_id):
        raise HTTPException(403, "Нет доступа к приватному каналу")
    await manager.leave_all_voice(profile_id)
    manager.voice_presence.setdefault(channel_id, {})[profile_id] = {
        "joined_at": _now(), "muted": False, "deaf": False, "speaking": False
    }
    await manager.broadcast_to_server(server_id, {"type": "room_joined", "server_id": server_id,
                                                  "channel_id": channel_id, "profile_id": profile_id})
    return JSONResponse({"ok": True, "members": list(manager.voice_presence.get(channel_id, {}).keys())})

@router.post("/servers/{server_id}/channels/{channel_id}/leave")
async def leave_voice_channel(server_id: str, channel_id: str, request: Request):
    profile_id = _get_active_profile(request)
    manager.voice_presence.get(channel_id, {}).pop(profile_id, None)
    await manager.broadcast_to_server(server_id, {"type": "room_left", "server_id": server_id,
                                                  "channel_id": channel_id, "profile_id": profile_id})
    return JSONResponse({"ok": True})

def _voice_mod_can(conn, server_id, profile_id, target):
    role = _server_role(conn, server_id, profile_id)
    if not role or role not in ("owner", "admin", "mod"):
        raise HTTPException(403, "Нет прав модератора")
    if target == profile_id:
        raise HTTPException(400, "Нельзя управлять собой")
    trole = _server_role(conn, server_id, target)
    if not trole:
        raise HTTPException(404, "Участник не найден")
    if trole in ("owner", "admin") and role != "owner":
        raise HTTPException(403, "Нельзя управлять этим участником")
    return True

@router.put("/servers/{server_id}/channels/{channel_id}/room-member")
async def room_mute_member(server_id: str, channel_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target = data.get("target") or ""
    conn = _get_db()
    _voice_mod_can(conn, server_id, profile_id, target)
    ch = conn.execute("SELECT * FROM social_server_channels WHERE id=? AND server_id=?",
                      (channel_id, server_id)).fetchone()
    if not ch or ch["type"] != "voice":
        raise HTTPException(404, "Голосовой канал не найден")
    presence = manager.voice_presence.get(channel_id, {})
    if target not in presence:
        raise HTTPException(400, "Участник не в комнате")
    st = presence[target]
    st["muted"] = bool(data.get("muted"))
    if st["muted"]:
        st["speaking"] = False
    await manager.broadcast_to_server(server_id, {"type": "room_state", "channel_id": channel_id,
                                                  "profile_id": target, "muted": st["muted"],
                                                  "deaf": st["deaf"], "speaking": st["speaking"]})
    return JSONResponse({"ok": True})

@router.post("/servers/{server_id}/channels/{channel_id}/room-kick")
async def room_kick_member(server_id: str, channel_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target = data.get("target") or ""
    conn = _get_db()
    _voice_mod_can(conn, server_id, profile_id, target)
    if target not in manager.voice_presence.get(channel_id, {}):
        raise HTTPException(400, "Участник не в комнате")
    manager.voice_presence.get(channel_id, {}).pop(target, None)
    await manager.broadcast_to_server(server_id, {"type": "room_left", "server_id": server_id,
                                                  "channel_id": channel_id, "profile_id": target})
    await manager.send_to_profile(target, {"type": "room_kicked", "server_id": server_id,
                                          "channel_id": channel_id, "profile_id": target})
    return JSONResponse({"ok": True})

@router.post("/servers/{server_id}/channels/{channel_id}/room-move")
async def room_move_member(server_id: str, channel_id: str, request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    target = data.get("target") or ""
    to_channel = data.get("to_channel") or ""
    conn = _get_db()
    _voice_mod_can(conn, server_id, profile_id, target)
    if target not in manager.voice_presence.get(channel_id, {}):
        raise HTTPException(400, "Участник не в комнате")
    if not to_channel or to_channel == channel_id:
        raise HTTPException(400, "Укажите другой канал")
    ch2 = conn.execute("SELECT * FROM social_server_channels WHERE id=? AND server_id=?",
                       (to_channel, server_id)).fetchone()
    if not ch2 or ch2["type"] != "voice":
        raise HTTPException(404, "Целевой канал не найден")
    if not _channel_access_ok(ch2, target):
        raise HTTPException(403, "Участник не имеет доступа к целевому каналу")
    st = manager.voice_presence[channel_id].pop(target)
    st["joined_at"] = _now()
    manager.voice_presence.setdefault(to_channel, {})[target] = st
    await manager.broadcast_to_server(server_id, {"type": "room_left", "server_id": server_id,
                                                  "channel_id": channel_id, "profile_id": target})
    await manager.broadcast_to_server(server_id, {"type": "room_joined", "server_id": server_id,
                                                  "channel_id": to_channel, "profile_id": target})
    await manager.send_to_profile(target, {"type": "room_moved", "server_id": server_id, "to_channel": to_channel})
    return JSONResponse({"ok": True})

# --- Voice Upload ---

@router.post("/upload/voice")
async def upload_voice(request: Request, file: UploadFile = File(...)):
    profile_id = _get_active_profile(request)
    ext = os.path.splitext(file.filename or ".webm")[1].lower()
    if ext not in {'.webm', '.ogg', '.wav', '.mp3', '.m4a'}:
        ext = '.webm'
    filename = f"{str(uuid.uuid4())}{ext}"
    filepath = os.path.join(VOICE_DIR, filename)

    def _save():
        with open(filepath, "wb") as f:
            shutil.copyfileobj(file.file, f)

    # запись загруженного файла — ждём диск, а не весь event loop
    import asyncio
    await asyncio.to_thread(_save)
    return JSONResponse({"ok": True, "path": f"/api/social/file/voice/{filename}"})

# --- WebSocket for online status ---

class ConnectionManager:
    def __init__(self):
        # Основная (последняя) вкладка профиля. Оставлено для совместимости
        # с кодом, который проверяет «в сети ли» через .get().
        self.active_connections: dict[str, WebSocket] = {}
        # Все открытые сокеты профиля. Без этого вторая вкладка того же
        # пользователя перезаписывала первую в active_connections, и
        # рассылка переставала приходить в первую вкладку.
        self.all_connections: dict[str, set] = {}
        self.group_subscriptions: dict[str, set] = {}
        self.server_subs: dict[str, set] = {}
        self.voice_presence: dict[str, dict] = {}

    async def connect(self, profile_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[profile_id] = websocket
        self.all_connections.setdefault(profile_id, set()).add(websocket)
        # Notify friends
        conn = _get_db()
        conn.execute("UPDATE social_users SET is_online=1 WHERE profile_id=?", (profile_id,))
        conn.commit()

    async def disconnect(self, profile_id: str, websocket: WebSocket = None):
        socks = self.all_connections.get(profile_id)
        still_online = False
        if socks:
            if websocket is not None:
                socks.discard(websocket)
            else:
                socks.clear()
            if socks:
                # Осталась другая вкладка: профиль всё ещё в сети.
                self.active_connections[profile_id] = next(iter(socks))
                still_online = True
            else:
                self.all_connections.pop(profile_id, None)
                self.active_connections.pop(profile_id, None)
        else:
            self.active_connections.pop(profile_id, None)
        await self.leave_all_voice(profile_id)
        if not still_online:
            conn = _get_db()
            conn.execute("UPDATE social_users SET is_online=0 WHERE profile_id=?", (profile_id,))
            conn.commit()

    async def send_to_profile(self, profile_id: str, message: dict):
        """Отправить во все вкладки профиля.

        Возвращает True, если дошло хотя бы до одного сокета.
        """
        socks = self.all_connections.get(profile_id)
        if not socks:
            return False
        sent = False
        for ws in list(socks):
            try:
                await ws.send_json(message)
                sent = True
            except Exception:
                swallow.note('social_module.send_to_profile:L6547')
                socks.discard(ws)
        return sent

    async def broadcast_to_group(self, group_id: str, message: dict,
                                 exclude_profile_id: str = None):
        """Рассылка всем участникам группы, у которых открыт сокет.

        Раньше такой рассылки не было: group_chat обходил members вручную
        прямо в обработчике отправки сообщения, а для постов, тем форума и
        репостов живых обновлений не было вовсе.
        """
        if not group_id:
            return
        conn = _get_db()
        members = conn.execute(
            "SELECT profile_id FROM social_group_members WHERE group_id=?", (group_id,)).fetchall()
        for m in members:
            pid = m["profile_id"]
            if pid == exclude_profile_id:
                continue
            await self.send_to_profile(pid, message)

    async def broadcast_to_conversation_members(self, conversation_id: str, message: dict,
                                                 exclude_profile_id: str = None):
        """Рассылка всем участникам диалога.

        broadcast_to_friends для «печатает…» не годится: он шлёт только
        принятым друзьям, поэтому в групповом диалоге (3+ участника)
        индикатор печати не доходил до собеседника, который ещё не
        подписан в друзьях. Здесь берём список прямо из таблицы членов.
        """
        if not conversation_id:
            return
        conn = _get_db()
        members = conn.execute(
            "SELECT profile_id FROM social_conversation_members WHERE conversation_id=?",
            (conversation_id,)
        ).fetchall()
        for m in members:
            pid = m["profile_id"]
            if pid == exclude_profile_id:
                continue
            await self.send_to_profile(pid, message)

    async def broadcast_to_server(self, server_id: str, message: dict):
        subs = self.server_subs.get(server_id, set())
        for pid in list(subs):
            await self.send_to_profile(pid, message)

    async def leave_all_voice(self, profile_id: str, server_id: str = None):
        for ch_id, members in list(self.voice_presence.items()):
            if profile_id in members:
                members.pop(profile_id, None)
                if not members:
                    self.voice_presence.pop(ch_id, None)
                ch = _get_db().execute(
                    "SELECT server_id FROM social_server_channels WHERE id=?", (ch_id,)
                ).fetchone()
                if ch and (not server_id or ch["server_id"] == server_id):
                    await self.broadcast_to_server(ch["server_id"], {
                        "type": "room_left", "server_id": ch["server_id"],
                        "channel_id": ch_id, "profile_id": profile_id
                    })

    async def broadcast_to_friends(self, profile_id: str, message: dict, conversation_id: str = None):
        conn = _get_db()
        friends = conn.execute(
            "SELECT CASE WHEN requester_id=? THEN addressee_id ELSE requester_id END AS fid "
            "FROM social_friendships WHERE (requester_id=? OR addressee_id=?) AND status='accepted'",
            (profile_id, profile_id, profile_id)
        ).fetchall()
        for f in friends:
            fid = f["fid"]
            if fid not in self.all_connections:
                continue
            if conversation_id:
                is_member = conn.execute(
                    "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
                    (conversation_id, fid)
                ).fetchone()
                if not is_member:
                    continue
            await self.send_to_profile(fid, message)

manager = ConnectionManager()

@router.websocket("/ws")
async def social_websocket(websocket: WebSocket):
    """WS-подключение. Личность берётся из cookie профиля (нельзя подделать через query).

    Глобальный активный профиль здесь больше не используется: он один на
    процесс и меняется навигацией любого браузера, из-за чего входящие
    события уходили не тому, кто открыл страницу. Запрос с чужого origin
    (CSWSH) отклоняем до подключения к менеджеру.
    """
    from local_request import origin_allowed
    if not origin_allowed(websocket.headers.get("origin", ""),
                          websocket.headers.get("host", "")):
        await websocket.accept()
        await websocket.close(code=4403)
        return
    try:
        import profiles_module as PM
        profile_id = PM.cookie_profile(websocket) or PM.resolve_profile_id("")
    except Exception:
        swallow.note('social_module.social_websocket:L6653')
        profile_id = None
    if not profile_id:
        await websocket.accept()
        await websocket.close(code=4401)
        return
    await manager.connect(profile_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "typing":
                    conv_id = msg.get("conversation_id")
                    # Отправитель обязан быть участником диалога, иначе
                    # он слал бы «печатает…» в чужой диалог по угаданному id.
                    _conn = _get_db()
                    _is_member = _conn.execute(
                        "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
                        (conv_id, profile_id)
                    ).fetchone()
                    if _is_member:
                        await manager.broadcast_to_conversation_members(conv_id, {
                            "type": "typing",
                            "conversation_id": conv_id,
                            "profile_id": profile_id
                        }, exclude_profile_id=profile_id)
                elif msg.get("type") == "typing_stopped":
                    conv_id = msg.get("conversation_id")
                    _conn = _get_db()
                    _is_member = _conn.execute(
                        "SELECT 1 FROM social_conversation_members WHERE conversation_id=? AND profile_id=?",
                        (conv_id, profile_id)
                    ).fetchone()
                    if _is_member:
                        await manager.broadcast_to_conversation_members(conv_id, {
                            "type": "typing_stopped",
                            "conversation_id": conv_id,
                            "profile_id": profile_id
                        }, exclude_profile_id=profile_id)
                elif msg.get("type") == "subscribe_group":
                    # Subscribe to group chat updates
                    conn = _get_db()
                    membership = conn.execute(
                        "SELECT 1 FROM social_group_members WHERE group_id=? AND profile_id=?",
                        (msg["group_id"], profile_id)
                    ).fetchone()
                    if membership:
                        if not hasattr(manager, "group_subscriptions"):
                            manager.group_subscriptions = {}
                        if profile_id not in manager.group_subscriptions:
                            manager.group_subscriptions[profile_id] = set()
                        manager.group_subscriptions[profile_id].add(msg["group_id"])
                elif msg.get("type") == "unsubscribe_group":
                    if hasattr(manager, "group_subscriptions") and profile_id in manager.group_subscriptions:
                        manager.group_subscriptions[profile_id].discard(msg.get("group_id"))
                elif msg.get("type") == "subscribe_server":
                    sid = msg.get("server_id")
                    if sid:
                        conn = _get_db()
                        if _server_role(conn, sid, profile_id):
                            manager.server_subs.setdefault(sid, set()).add(profile_id)
                elif msg.get("type") == "unsubscribe_server":
                    sid = msg.get("server_id")
                    if sid and sid in manager.server_subs:
                        manager.server_subs[sid].discard(profile_id)
                elif msg.get("type") == "room_state":
                    ch_id = msg.get("channel_id")
                    if ch_id and ch_id in manager.voice_presence and profile_id in manager.voice_presence[ch_id]:
                        st = manager.voice_presence[ch_id][profile_id]
                        if "muted" in msg:
                            st["muted"] = bool(msg["muted"])
                        if "deaf" in msg:
                            st["deaf"] = bool(msg["deaf"])
                        if "speaking" in msg:
                            st["speaking"] = bool(msg["speaking"]) and not st.get("muted")
                        ch = _get_db().execute(
                            "SELECT server_id FROM social_server_channels WHERE id=?", (ch_id,)
                        ).fetchone()
                        if ch:
                            await manager.broadcast_to_server(ch["server_id"], {
                                "type": "room_state", "channel_id": ch_id, "profile_id": profile_id,
                                "muted": st["muted"], "deaf": st["deaf"], "speaking": st["speaking"]
                            })
                elif msg.get("type") == "room_typing":
                    ch_id = msg.get("channel_id")
                    if ch_id:
                        ch = _get_db().execute(
                            "SELECT server_id FROM social_server_channels WHERE id=?", (ch_id,)
                        ).fetchone()
                        if ch:
                            await manager.broadcast_to_server(ch["server_id"], {
                                "type": "room_typing", "channel_id": ch_id, "profile_id": profile_id
                            })
            except json.JSONDecodeError:
                swallow.note('social_module.social_websocket:L6748')
                pass
    except WebSocketDisconnect:
        # Передаём конкретный сокет: иначе закрытие одной вкладки снимало
        # профиль из сети целиком, и вторая вкладка оставалась без сообщений.
        swallow.note('social_module.social_websocket:L6751')
        await manager.disconnect(profile_id, websocket)
    except Exception:
        swallow.note('social_module.social_websocket:L6756')
        await manager.disconnect(profile_id, websocket)

# --- File serving ---

@router.get("/file/{folder}/{filename}")
def serve_social_file(folder: str, filename: str):
    if folder == "avatars":
        base = AVATARS_DIR
    elif folder == "covers":
        base = COVERS_DIR
    elif folder == "post_media":
        base = POST_MEDIA_DIR
    elif folder == "stories":
        base = STORIES_DIR
    elif folder == "voice":
        base = VOICE_DIR
    elif folder == "chat_media":
        base = CHAT_MEDIA_DIR
    elif folder == "stickers":
        base = STICKERS_DIR
    elif folder == "root":
        base = UPLOADS_DIR
    else:
        raise HTTPException(404, "Неизвестная папка")
    filepath = os.path.join(base, filename)
    if not os.path.exists(filepath):
        raise HTTPException(404, "Файл не найден")
    if os.path.basename(filepath) != filename or filename in ("", ".", ".."):
        raise HTTPException(400, "Недопустимое имя файла")
    media_type, _ = mimetypes.guess_type(filepath)
    media_type = media_type or "application/octet-stream"
    ext = os.path.splitext(filename)[1].lower()
    inline = ext in (_ALLOWED_IMAGE_EXT | {'.txt'})
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(os.path.basename(filename))}",
    }
    return FileResponse(filepath, media_type=media_type, headers=headers)

@router.get("/file/{filename}")
def serve_social_file_flat(filename: str):
    # filename приходит из URL: на Windows "..\\" тоже разделитель, поэтому
    # проверяем до склейки, а не после (иначе os.path.exists уже смотрит наружу).
    if os.path.basename(filename) != filename or filename in ("", ".", ".."):
        raise HTTPException(400, "Недопустимое имя файла")
    for base_name, base_dir in [
        ("avatars", AVATARS_DIR), ("covers", COVERS_DIR),
        ("post_media", POST_MEDIA_DIR), ("voice", VOICE_DIR),
        ("chat_media", CHAT_MEDIA_DIR), ("stickers", STICKERS_DIR),
        ("root", UPLOADS_DIR), ("stories", STORIES_DIR),
    ]:
        fp = os.path.join(base_dir, filename)
        if os.path.exists(fp):
            media_type, _ = mimetypes.guess_type(fp)
            media_type = media_type or "application/octet-stream"
            ext = os.path.splitext(filename)[1].lower()
            inline = ext in (_ALLOWED_IMAGE_EXT | {'.txt'})
            headers = {
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(os.path.basename(filename))}",
            }
            return FileResponse(fp, media_type=media_type, headers=headers)
    raise HTTPException(404, "Файл не найден")

# --- Federation Search API ---

def _is_lan_or_ip(domain: str) -> bool:
    """Эвристика: LAN/IP адрес → работать по http (без 10-сек таймаута на https)."""
    d = (domain or "").strip().lower()
    if d.startswith("localhost") or d.startswith("127.") or d.startswith("192.168.") or d.startswith("10."):
        return True
    host = d.rsplit(":", 1)[0] if ":" in d else d
    return host.replace(".", "").replace("-", "").isdigit()


def _get_instance_protocol(domain: str, conn=None) -> str:
    """Протокол для домена: из таблицы инстансов, иначе по эвристике LAN/IP."""
    if conn:
        row = conn.execute(
            "SELECT protocol FROM social_ap_instances WHERE domain=?", (domain,)
        ).fetchone()
        if row and row["protocol"]:
            return row["protocol"]
    return "http" if _is_lan_or_ip(domain) else "https"

@router.get("/ap/search")
async def search_federation(q: str = Query("")):
    """Search for users on other ActivityPub servers (Mastodon, Pleroma, etc.)"""
    if not q or '@' not in q:
        return JSONResponse([])
    # Parse username@domain
    parts = q.strip().split('@')
    if len(parts) < 2:
        return JSONResponse([])
    username = parts[0].strip()
    domain = parts[1].strip()
    if not username or not domain:
        return JSONResponse([])
    
    # Check cache first
    conn = _get_db()
    cached = conn.execute(
        "SELECT * FROM social_ap_cache WHERE username=? AND domain=?",
        (username, domain)
    ).fetchone()
    if cached and cached["cached_at"] > time.time() - 3600:
        return JSONResponse([_row_to_dict(cached)])
    
    # WebFinger lookup
    scheme = _get_instance_protocol(domain, conn)
    webfinger_url = f"{scheme}://{domain}/.well-known/webfinger?resource=acct:{username}@{domain}"
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            wf_resp = await client.get(webfinger_url, headers={"Accept": "application/json"})
            if wf_resp.status_code != 200:
                # Try other scheme as fallback (https for LAN, or vice versa)
                fallback_scheme = "https" if scheme == "http" else "http"
                webfinger_url = f"{fallback_scheme}://{domain}/.well-known/webfinger?resource=acct:{username}@{domain}"
                wf_resp = await client.get(webfinger_url, headers={"Accept": "application/json"})
                if wf_resp.status_code == 200:
                    scheme = fallback_scheme
            if wf_resp.status_code != 200:
                return JSONResponse([])
            wf_data = wf_resp.json()

            # Remember verified scheme for this domain
            conn.execute(
                "INSERT INTO social_ap_instances (domain, protocol, name, verified_at, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET protocol=excluded.protocol, verified_at=excluded.verified_at",
                (domain, scheme, "", time.time(), time.time())
            )
            conn.commit()
            
            # Find actor URL
            actor_url = ""
            for link in wf_data.get("links", []):
                if link.get("rel") == "self" and link.get("type") in ("application/activity+json", "application/ld+json; profile=\"https://www.w3.org/ns/activitystreams\""):
                    actor_url = link.get("href", "")
                    break
            if not actor_url:
                return JSONResponse([])
            
            # Fetch actor
            actor_resp = await client.get(actor_url, headers={"Accept": "application/activity+json"})
            if actor_resp.status_code != 200:
                return JSONResponse([])
            actor = actor_resp.json()
            
            result = {
                "actor_id": actor.get("id", actor_url),
                "username": username,
                "domain": domain,
                "inbox_url": actor.get("inbox", ""),
                "outbox_url": actor.get("outbox", ""),
                "public_key_pem": actor.get("publicKey", {}).get("publicKeyPem", ""),
                "avatar_url": "",
                "display_name": actor.get("name", ""),
                "summary": actor.get("summary", ""),
                "followers_url": actor.get("followers", ""),
                "following_url": actor.get("following", ""),
            }
            # Extract avatar from icon
            icon = actor.get("icon") or {}
            if isinstance(icon, dict):
                result["avatar_url"] = icon.get("url", "")
            elif isinstance(icon, list) and len(icon):
                result["avatar_url"] = icon[0].get("url", "")
            
            # Cache
            conn.execute(
                "INSERT OR REPLACE INTO social_ap_cache (actor_id, username, domain, inbox_url, outbox_url, public_key_pem, avatar_url, display_name, summary, followers_url, following_url, cached_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (result["actor_id"], result["username"], result["domain"], result["inbox_url"], result["outbox_url"], result["public_key_pem"], result["avatar_url"], result["display_name"], result["summary"], result["followers_url"], result["following_url"], time.time())
            )
            conn.commit()
            return JSONResponse([result])
    except Exception:
        swallow.note('social_module.search_federation:L6935')
        return JSONResponse([])

# --- Federation interactions (follow / unfollow / profile / directory) ---

@router.post("/ap/follow")
def follow_remote_user(request: Request, data: dict = Body(...)):
    """Подписаться на пользователя с другого ActivityPub-сервера."""
    profile_id = _get_active_profile(request)
    _ensure_user(profile_id)
    actor_url = (data.get("actor_url") or "").strip()
    if not actor_url:
        raise HTTPException(400, "Не указан actor_url")
    remote = None
    try:
        import activitypub_module as apm
        local = apm.get_or_create_local_actor(profile_id, _get_profile_name(profile_id))
        remote = apm.resolve_remote_actor(actor_url)
        ok = bool(local and remote and apm.federate_follow(profile_id, actor_url)) if remote else False
    except Exception as e:
        logger.warning(f"AP follow error: {e}")
        ok = False
    if not remote:
        raise HTTPException(404, "Не удалось получить профиль удалённого пользователя")
    username = remote.get("username") or ""
    domain = remote.get("domain") or (urlparse(actor_url).netloc if actor_url else "")
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO social_remote_follows (profile_id, actor_url, username, domain, display_name, avatar_url, summary, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (profile_id, actor_url, username, domain,
         remote.get("display_name", "") or username,
         remote.get("avatar_url", "") or "",
         remote.get("summary", "") or "",
         "pending" if ok else "failed", _now())
    )
    conn.commit()
    return JSONResponse({"ok": ok, "status": "pending" if ok else "failed"})

@router.delete("/ap/follow")
def unfollow_remote_user(request: Request, data: dict = Body(...)):
    profile_id = _get_active_profile(request)
    actor_url = (data.get("actor_url") or "").strip()
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM social_remote_follows WHERE profile_id=? AND actor_url=?",
        (profile_id, actor_url)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM social_remote_follows WHERE profile_id=? AND actor_url=?", (profile_id, actor_url))
        conn.commit()
    try:
        import activitypub_module as apm
        apm.federate_unfollow(profile_id, actor_url)
    except Exception:
        swallow.note('social_module.unfollow_remote_user:L6989')
        pass
    return JSONResponse({"ok": True})

@router.get("/ap/following")
def get_remote_following(request: Request):
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM social_remote_follows WHERE profile_id=? ORDER BY created_at DESC",
        (profile_id,)
    ).fetchall()
    return JSONResponse([_row_to_dict(r) for r in rows])

@router.get("/ap/profile")
def get_remote_profile(actor: str = Query("")):
    if not actor:
        raise HTTPException(400, "Не указан actor")
    try:
        import activitypub_module as apm
        data = apm.resolve_remote_actor(actor)
    except Exception as e:
        logger.warning(f"AP profile resolve error: {e}")
        data = None
    if not data:
        raise HTTPException(404, "Профиль не найден")
    return JSONResponse(dict(data))

@router.get("/ap/directory")
def federation_directory():
    """Каталог известных феди-инстансов + популярные публичные серверы."""
    conn = _get_db()
    domains = conn.execute("SELECT DISTINCT domain FROM social_ap_cache").fetchall()
    known = [d["domain"] for d in domains if d["domain"]]
    suggestions = [
        {"domain": "mastodon.social", "name": "Mastodon Social"},
        {"domain": "mastodon.online", "name": "Mastodon Online"},
        {"domain": "mastodon.world", "name": "Mastodon World"},
        {"domain": "mastodon.cloud", "name": "Mastodon Cloud"},
        {"domain": "mstdn.social", "name": "Mstdn Social"},
        {"domain": "mastodon.ml", "name": "Mastodon ML"},
        {"domain": "mastodon.ar.al", "name": "ARAL'S Mastodon"},
        {"domain": "kolektiva.social", "name": "Kolektiva"},
        {"domain": "friendica.me", "name": "Friendica"},
        {"domain": "misskey.io", "name": "Misskey.io"},
    ]
    return JSONResponse({"known": known, "suggestions": suggestions})

# --- Instance discovery (LAN pairing / gossip) ---

def _ap_base_for(addr: str) -> str:
    """Нормализует адрес инстанса в https://host:port (без trailing slash)."""
    addr = (addr or "").strip().rstrip("/")
    if not addr:
        return ""
    if "://" not in addr:
        addr = f"{'http' if _is_lan_or_ip(addr) else 'https'}://{addr}"
    return addr

def _merge_peers(conn, peer_list, from_domain=""):
    """Списывает чужие известные инстансы в свою таблицу social_ap_peers."""
    now = time.time()
    merged = 0
    for p in peer_list or []:
        dom = (p.get("domain") or "").strip()
        if not dom or dom in ("localhost",) or dom == from_domain:
            continue
        conn.execute(
            "INSERT INTO social_ap_peers (domain, name, last_seen, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(domain) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
            (dom, (p.get("name") or ""), now, now)
        )
        merged += 1
    return merged

@router.get("/ap/instances")
def list_instances(request: Request):
    """Все известные инстансы (добавленные + из gossip-обмена)."""
    conn = _get_db()
    inst = conn.execute("SELECT domain, protocol, name, verified_at FROM social_ap_instances ORDER BY created_at").fetchall()
    peers = conn.execute("SELECT domain, name, last_seen FROM social_ap_peers ORDER BY name").fetchall()
    result = []
    known_domains = {r["domain"] for r in inst}
    for r in inst:
        result.append({"domain": r["domain"], "protocol": r["protocol"], "name": r["name"] or r["domain"], "source": "saved", "verified_at": r["verified_at"]})
    for p in peers:
        if p["domain"] not in known_domains:
            result.append({"domain": p["domain"], "protocol": "http" if _is_lan_or_ip(p["domain"]) else "https", "name": p["name"] or p["domain"], "source": "peer", "last_seen": p["last_seen"]})
    return JSONResponse(result)

@router.post("/ap/instances")
async def add_instance(request: Request, data: dict = Body(...)):
    """Добавить инстанс по адресу (например http://192.168.1.10:8080)."""
    profile_id = _get_active_profile(request)
    addr = _ap_base_for(data.get("address"))
    if not addr:
        raise HTTPException(400, "Некорректный адрес")
    parsed = urlparse(addr)
    domain = parsed.netloc
    conn = _get_db()
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            node = await client.get(f"{addr}/.well-known/nodeinfo", headers={"Accept": "application/json"})
            name = parsed.netloc
            if node.status_code == 200:
                try:
                    links = node.json().get("links", [])
                    if links:
                        ni = await client.get(links[0].get("href", ""), headers={"Accept": "application/json"})
                        if ni.status_code == 200:
                            d = ni.json()
                            s = d.get("software") or {}
                            name = f"{(d.get('metadata') or {}).get('nodeName') or parsed.netloc} ({s.get('name') or 'infinitysocial'})"
                except Exception:
                    swallow.note('social_module.add_instance:L7103')
                    pass
            conn.execute(
                "INSERT INTO social_ap_instances (domain, protocol, name, verified_at, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET protocol=excluded.protocol, name=excluded.name, verified_at=excluded.verified_at",
                (domain, parsed.scheme, name, time.time(), time.time())
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Add instance error: {e}")
        # Сохраним даже непроверенный — юзер мог ошибиться в адресе
        conn.execute(
            "INSERT INTO social_ap_instances (domain, protocol, name, verified_at, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(domain) DO UPDATE SET protocol=excluded.protocol, verified_at=excluded.verified_at",
            (domain, parsed.scheme, parsed.netloc, 0, time.time())
        )
        conn.commit()
        raise HTTPException(400, "Инстанс недоступен. Проверьте адрес")
    return JSONResponse({"ok": True, "domain": domain})

@router.delete("/ap/instances")
def remove_instance(request: Request, domain: str = Query("")):
    conn = _get_db()
    conn.execute("DELETE FROM social_ap_instances WHERE domain=?", (domain,))
    conn.execute("DELETE FROM social_ap_peers WHERE domain=?", (domain,))
    conn.commit()
    return JSONResponse({"ok": True})

# --- Pairing (децентрализованный "мессенджер" уровень 2: код через сторонний чат) ---

_PAIR_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

def _gen_pair_code() -> str:
    import secrets
    return "".join(secrets.choice(_PAIR_CHARS) for _ in range(8))

@router.post("/ap/pair/create")
def pair_create(request: Request):
    """Создать код подключения (живёт 10 минут, одноразовый)."""
    profile_id = _get_active_profile(request)
    import activitypub_module as apm
    code = _gen_pair_code()
    now = time.time()
    conn = _get_db()
    conn.execute(
        "INSERT INTO social_ap_pairing (code, local_domain, created_at, expires_at, status) VALUES (?,?,?,?,?)",
        (code, apm.get_domain(), now, now + 600, "pending")
    )
    conn.commit()
    base = apm._base_url()
    return JSONResponse({
        "code": code,
        "address": base,
        "expires_in": 600,
        "message": f"Отправьте другу эти данные через любой мессенджер: Код {code} · Адрес {base}"
    })

@router.post("/ap/pair/verify")
async def pair_verify(body: dict = Body(...)):
    """Публичный endpoint: второй инстанс присылает код + свой адрес для подтверждения."""
    code = (body.get("code") or "").strip().upper()
    remote_addr = _ap_base_for(body.get("address"))
    remote_name = (body.get("name") or "").strip()
    if not code or not remote_addr:
        raise HTTPException(400, "Код и адрес обязательны")
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM social_ap_pairing WHERE code=? AND status='pending'", (code,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Неверный или использованный код")
    if row["expires_at"] < time.time():
        conn.execute("UPDATE social_ap_pairing SET status='expired' WHERE code=?", (code,))
        conn.commit()
        raise HTTPException(410, "Код истёк. Попросите новый")
    # Принять: запомнить пира, пометить код использованным
    parsed = urlparse(remote_addr)
    domain = parsed.netloc
    now = time.time()
    conn.execute(
        "UPDATE social_ap_pairing SET status='used', remote_domain=?, remote_name=? WHERE code=?",
        (domain, remote_name, code)
    )
    conn.execute(
        "INSERT INTO social_ap_instances (domain, protocol, name, verified_at, created_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET protocol=excluded.protocol, name=excluded.name, verified_at=excluded.verified_at",
        (domain, parsed.scheme, remote_name or domain, now, now)
    )
    # Обмен gossip: забрать у пира его известные инстансы
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            r = await client.get(f"{remote_addr}/api/social/ap/peers", headers={"Accept": "application/json"})
            if r.status_code == 200:
                _merge_peers(conn, r.json().get("instances", []), from_domain=domain)
    except Exception:
        swallow.note('social_module.pair_verify:L7198')
        pass
    # Записать пира в gossip-таблицу
    conn.execute(
        "INSERT INTO social_ap_peers (domain, name, last_seen, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
        (domain, remote_name or domain, now, now)
    )
    conn.commit()
    return JSONResponse({"ok": True, "paired_with": domain})

@router.post("/ap/pair/connect")
async def pair_connect(request: Request, data: dict = Body(...)):
    """Ввести код, полученный от друга → соединиться с его инстансом."""
    code = (data.get("code") or "").strip().upper()
    address = _ap_base_for(data.get("address"))
    if not code or not address:
        raise HTTPException(400, "Укажите код и адрес инстанса")
    import activitypub_module as apm
    parsed = urlparse(address)
    domain = parsed.netloc
    profile_id = _get_active_profile(request)
    conn = _get_db()
    # Вызвать чужой verify
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            r = await client.post(
                f"{address}/api/social/ap/pair/verify",
                json={"code": code, "address": apm._base_url(), "name": apm.get_domain()}
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, "Инстанс друга не принял код: проверьте адрес/код")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Не удалось связаться с {address}")
    # Со своей стороны запомнить друга
    now = time.time()
    conn.execute(
        "INSERT INTO social_ap_instances (domain, protocol, name, verified_at, created_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET protocol=excluded.protocol, verified_at=excluded.verified_at",
        (domain, parsed.scheme, domain, now, now)
    )
    conn.execute(
        "INSERT INTO social_ap_peers (domain, name, last_seen, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
        (domain, domain, now, now)
    )
    conn.commit()
    return JSONResponse({"ok": True, "paired_with": domain})

# --- Peers (gossip) ---

@router.get("/ap/peers")
def get_peers():
    conn = _get_db()
    rows = conn.execute(
        "SELECT domain, protocol, name FROM social_ap_instances "
        "UNION SELECT domain, '', name FROM social_ap_peers"
    ).fetchall()
    return JSONResponse({"instances": [{ "domain": r["domain"], "protocol": r["protocol"], "name": r["name"] or r["domain"] } for r in rows]})

@router.post("/ap/peers/refresh")
async def refresh_peers(request: Request):
    """Опрос всех известных инстансов — слить их gossip-списки."""
    profile_id = _get_active_profile(request)
    conn = _get_db()
    rows = conn.execute("SELECT domain, protocol FROM social_ap_instances").fetchall()
    domains = [("http" if not r["protocol"] else r["protocol"], r["domain"]) for r in rows]
    total = 0
    async def _fetch(base):
        try:
            async with httpx.AsyncClient(timeout=6.0, verify=False) as client:
                r = await client.get(f"{base}/api/social/ap/peers", headers={"Accept": "application/json"})
                if r.status_code == 200:
                    return r.json().get("instances", [])
        except Exception:
            swallow.note('social_module._fetch:L7275')
            pass
        return []
    import asyncio
    results = await asyncio.gather(*[_fetch(f"{proto}://{dom}") for proto, dom in domains])
    for lst in results:
        total += _merge_peers(conn, lst)
    conn.commit()
    return JSONResponse({"ok": True, "merged": total})

# --- Remote user search across known instances ---

@router.get("/ap/search-instances")
async def search_instances(username: str = Query(""), instance: str = Query("")):
    """Искать username на всех известных инстансах (или на конкретном). Пустой username + instance = каталог юзеров пира."""
    conn = _get_db()
    rows = conn.execute("SELECT domain FROM social_ap_instances").fetchall()
    if instance:
        domains = [r["domain"] for r in rows if r["domain"].lower() == instance.strip().lower()]
        if not domains and instance.strip():
            domains = [instance.strip()]
    else:
        domains = [r["domain"] for r in rows]
    if not username and not domains:
        return JSONResponse([])
    result = []

    async def _one(domain):
        try:
            scheme = _get_instance_protocol(domain, conn)
            async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
                if not username:
                    lu = await client.get(f"{scheme}://{domain}/api/social/ap/local-users", headers={"Accept": "application/json"})
                    if lu.status_code != 200:
                        return None
                    return [{
                        "actor_id": u.get("actor_url", ""),
                        "username": u.get("username", ""),
                        "domain": domain,
                        "display_name": u.get("display_name", ""),
                        "avatar_url": u.get("avatar_url", ""),
                        "summary": "",
                        "instance": domain,
                    } for u in lu.json() if u.get("actor_url")]
                wf = await client.get(f"{scheme}://{domain}/.well-known/webfinger?resource=acct:{username}@{domain}", headers={"Accept": "application/json"})
                if wf.status_code != 200:
                    alt = "https" if scheme == "http" else "http"
                    wf = await client.get(f"{alt}://{domain}/.well-known/webfinger?resource=acct:{username}@{domain}", headers={"Accept": "application/json"})
                if wf.status_code != 200:
                    return None
                actor_url = ""
                for link in wf.json().get("links", []):
                    if link.get("rel") == "self" and "activity+json" in link.get("type", ""):
                        actor_url = link.get("href", "")
                        break
                if not actor_url:
                    return None
                act = await client.get(actor_url, headers={"Accept": "application/activity+json"})
                if act.status_code != 200:
                    return None
                d = act.json()
                icon = d.get("icon") or {}
                av = icon.get("url", "") if isinstance(icon, dict) else (icon[0].get("url", "") if isinstance(icon, list) and icon else "")
                return [{
                    "actor_id": d.get("id", actor_url),
                    "username": d.get("preferredUsername") or username,
                    "domain": domain,
                    "display_name": d.get("name", ""),
                    "avatar_url": av,
                    "summary": d.get("summary", ""),
                    "inbox_url": d.get("inbox", ""),
                    "outbox_url": d.get("outbox", ""),
                    "followers_url": d.get("followers", ""),
                    "instance": domain,
                }]
        except Exception:
            swallow.note('social_module._one:L7351')
            return None

    import asyncio
    found = await asyncio.gather(*[_one(d) for d in domains])
    flat = []
    for f in found:
        if f:
            flat.extend(f)
    return JSONResponse(flat)

# --- Stats ---

@router.get("/stats")
def get_social_stats():
    conn = _get_db()
    users = conn.execute("SELECT COUNT(*) FROM social_users").fetchone()[0]
    posts = conn.execute("SELECT COUNT(*) FROM social_posts").fetchone()[0]
    comments = conn.execute("SELECT COUNT(*) FROM social_comments").fetchone()[0]
    friendships = conn.execute("SELECT COUNT(*) FROM social_friendships WHERE status='accepted'").fetchone()[0]
    messages = conn.execute("SELECT COUNT(*) FROM social_messages").fetchone()[0]
    return JSONResponse({
        "users": users, "posts": posts, "comments": comments,
        "friendships": friendships, "messages": messages
    })

# --- Роутер ботов ---

# Подключается в конце модуля, потому что social_bot_api импортирует social_module
# (нужны _get_db, _get_active_profile, _check_rate_limit, _server_role). Если импорт
# не удался - мессенджер работает как раньше, просто без ботов.
#
# Сам router.include_router здесь убран: у social_bot_api свой префикс
# /api/social, а main.py:1245 регистрирует этот роутер отдельно. Вложенная
# регистрация удваивала префикс и рождала второй, никому не нужный набор
# путей вида /api/social/api/social/bots (живой процесс отдавал и то, и другое).
try:
    import social_bot_api  # noqa: F401  (проверка доступности ботов)
except Exception as _bot_api_err:
    if logger:
        logger.warning("Роутер ботов не подключён: %s", _bot_api_err)

# Альбомы групп и каналов — отдельный модуль, потому что блок большой,
# а social_module.py уже перевалил за 6000 строк.
try:
    import social_group_albums
    router.include_router(social_group_albums.router)
except Exception as _albums_err:
    if logger:
        logger.warning("Роутер альбомов групп не подключён: %s", _albums_err)

# Статьи групп и каналов — отдельный модуль по той же причине.
try:
    import social_group_articles
    router.include_router(social_group_articles.router)
except Exception as _articles_err:
    if logger:
        logger.warning("Роутер статей не подключён: %s", _articles_err)

# Форум групп — отдельный модуль по той же причине.
try:
    import social_group_forum
    router.include_router(social_group_forum.router)
except Exception as _forum_err:
    if logger:
        logger.warning("Роутер форума не подключён: %s", _forum_err)

# Редактор фото — отдельный модуль: тянет PIL и держит свою логику версий.
try:
    import social_photo_edit
    router.include_router(social_photo_edit.router)
except Exception as _pedit_err:
    if logger:
        logger.warning("Роутер редактора фото не подключён: %s", _pedit_err)
