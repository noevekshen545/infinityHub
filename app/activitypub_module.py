"""ActivityPub С„РµРґРµСЂР°С†РёСЏ вЂ” РїРѕР»РЅР°СЏ СЃРѕРІРјРµСЃС‚РёРјРѕСЃС‚СЊ СЃ Mastodon/Pleroma/Misskey."""

import os, json, time, uuid, base64, hashlib, re, threading, logging, ipaddress, socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, List
from urllib.parse import urlparse

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
import httpx
import swallow

try:
    import server
    logger = server.logger
except ImportError:
    logger = logging.getLogger("ap")

# --- CONFIG ---

def _resolve_domain():
    env = os.environ.get("DOMAIN", "").strip()
    if env:
        return env
    try:
        from config_module import get_config
        d = (get_config().get("social_domain") or "").strip()
        if d:
            return d
    except Exception:
        swallow.note('activitypub_module._resolve_domain:L32')
        pass
    return "localhost:8080"


def _scheme(domain=None):
    d = (domain or _DOMAIN).lower()
    if d.startswith("localhost") or "127.0.0.1" in d:
        return "http"
    host = d.rsplit(":", 1)[0] if ":" in d else d
    # Raw-IP hosts (LAN / edge) default to http
    if host.replace(".", "").replace("-", "").isdigit():
        return "http"
    return "https"


_DOMAIN = _resolve_domain()


def _base_url():
    return f"{_scheme()}://{_DOMAIN}"


# TLS-проверка удалённых инстансов. Раньше везде стоял verify=False, из-за чего
# федеративные ответы можно было подменить MITM. Для самоподписанных узлов
# оставлен явный opt-out.
_AP_VERIFY = os.environ.get("INFINITY_AP_INSECURE", "").strip().lower() not in ("1", "true", "yes", "on")
# По умолчанию не ходим на loopback/LAN из федеративных URL (SSRF). Для
# домашней федерации по LAN — INFINITY_AP_ALLOW_PRIVATE=1.
_AP_ALLOW_PRIVATE = os.environ.get("INFINITY_AP_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")


def _remote_url_allowed(url: str) -> bool:
    if _AP_ALLOW_PRIVATE:
        return True
    try:
        p = urlparse(url)
    except Exception:
        swallow.note('activitypub_module._remote_url_allowed:L70')
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.strip("[]").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        swallow.note('activitypub_module._remote_url_allowed:L80')
        return True  # домен: DNS-резолв оставляем сетевому слою
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def get_domain() -> str:
    return _DOMAIN


def set_domain(domain: str):
    global _DOMAIN
    domain = (domain or "").strip()
    if domain:
        _DOMAIN = domain


DOMAIN = _DOMAIN  # back-compat alias (import-time value)
BASE_URL = _base_url()  # back-compat alias (import-time value)
SOCIAL_MODULE = None  # Set by main.py after import

_AP_OLD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "activitypub")
AP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".infinity", "data", "activitypub")
os.makedirs(AP_DATA_DIR, exist_ok=True)
AP_DB_PATH = os.path.join(AP_DATA_DIR, "activitypub.db")

_old_ap_db = os.path.join(_AP_OLD_DIR, "activitypub.db")
if os.path.exists(_old_ap_db) and not os.path.exists(AP_DB_PATH):
    import shutil
    shutil.copy2(_old_ap_db, AP_DB_PATH)
    logger.info(f"Migrated activitypub.db from {_old_ap_db} to {AP_DB_PATH}")

router = APIRouter(tags=["activitypub"])

# --- Database ---

_db_local = threading.local()

def _get_db():
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        import sqlite3 as _sqlite3
        _db_local.conn = _sqlite3.connect(AP_DB_PATH, timeout=10)
        _db_local.conn.row_factory = _sqlite3.Row
        try:
            _db_local.conn.execute("PRAGMA journal_mode=WAL")
            _db_local.conn.execute("PRAGMA wal_autocheckpoint=1000")
        except _sqlite3.OperationalError:
            swallow.note('activitypub_module._get_db:L127')
            pass
        _db_local.conn.execute("PRAGMA synchronous=NORMAL")
        _db_local.conn.execute("PRAGMA foreign_keys=ON")
        _db_local.conn.execute("PRAGMA busy_timeout=5000")
    return _db_local.conn

def _init_db():
    conn = _get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ap_actors (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        domain TEXT NOT NULL DEFAULT '',
        actor_url TEXT NOT NULL DEFAULT '',
        inbox_url TEXT NOT NULL DEFAULT '',
        shared_inbox_url TEXT DEFAULT '',
        followers_url TEXT DEFAULT '',
        following_url TEXT DEFAULT '',
        outbox_url TEXT DEFAULT '',
        public_key_pem TEXT DEFAULT '',
        private_key_pem TEXT DEFAULT '',
        display_name TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        avatar_url TEXT DEFAULT '',
        header_url TEXT DEFAULT '',
        profile_data TEXT DEFAULT '{}',
        is_local INTEGER DEFAULT 0,
        is_group INTEGER DEFAULT 0,
        last_refreshed REAL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        UNIQUE(username, domain)
    );
    CREATE TABLE IF NOT EXISTS ap_follows (
        id TEXT PRIMARY KEY,
        follower_id TEXT NOT NULL REFERENCES ap_actors(id),
        followed_id TEXT NOT NULL REFERENCES ap_actors(id),
        state TEXT NOT NULL DEFAULT 'pending',
        remote_activity_id TEXT DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL DEFAULT 0,
        UNIQUE(follower_id, followed_id)
    );
    CREATE TABLE IF NOT EXISTS ap_inbox (
        id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        activity_id TEXT UNIQUE,
        type TEXT NOT NULL,
        object_type TEXT DEFAULT '',
        object_id TEXT DEFAULT '',
        raw_json TEXT NOT NULL,
        is_processed INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ap_outbox (
        id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        activity_id TEXT UNIQUE,
        type TEXT NOT NULL,
        object_type TEXT DEFAULT '',
        object_id TEXT DEFAULT '',
        raw_json TEXT NOT NULL,
        is_delivered INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ap_delivery_queue (
        id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        inbox_url TEXT NOT NULL,
        activity_json TEXT NOT NULL,
        activity_id TEXT DEFAULT '',
        state TEXT DEFAULT 'pending',
        retries INTEGER DEFAULT 0,
        last_error TEXT DEFAULT '',
        created_at REAL NOT NULL
    );
    """)
    conn.commit()

try:
    _init_db()
except Exception as e:
    if logger:
        logger.warning(f"ActivityPub DB init error (module disabled): {e}")

# --- Crypto helpers ---

def _generate_keypair() -> tuple:
    """Generate RSA keypair for ActivityPub HTTP Signatures."""
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    return public_pem, private_pem

def _key_id(actor_url: str) -> str:
    return f"{actor_url}#main-key"

def _actor_url(username: str) -> str:
    return f"{_base_url()}/api/social/ap/actors/{username}"

def _inbox_url(username: str) -> str:
    return f"{_base_url()}/api/social/ap/actors/{username}/inbox"

def _shared_inbox_url() -> str:
    return f"{_base_url()}/api/social/ap/inbox"

def _followers_url(username: str) -> str:
    return f"{_base_url()}/api/social/ap/actors/{username}/followers"

def _following_url(username: str) -> str:
    return f"{_base_url()}/api/social/ap/actors/{username}/following"

def _outbox_url(username: str) -> str:
    return f"{_base_url()}/api/social/ap/actors/{username}/outbox"

# --- Actor creation/resolution ---

def get_or_create_local_actor(profile_id: str, display_name: str = "") -> dict:
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (profile_id,)
    ).fetchone()
    if row:
        return dict(row)
    # Create new local actor
    username = profile_id
    public_key, private_key = _generate_keypair()
    now = time.time()
    conn.execute("""
        INSERT INTO ap_actors (id, username, domain, actor_url, inbox_url, shared_inbox_url,
        followers_url, following_url, outbox_url, public_key_pem, private_key_pem,
        display_name, is_local, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        str(uuid.uuid4()), username, get_domain(), _actor_url(username),
        _inbox_url(username), _shared_inbox_url(),
        _followers_url(username), _following_url(username), _outbox_url(username),
        public_key, private_key, display_name or username,
        1, now, now
    ))
    conn.commit()
    row = conn.execute("SELECT * FROM ap_actors WHERE username=?", (username,)).fetchone()
    return dict(row) if row else {}

def resolve_remote_actor(actor_url: str) -> Optional[dict]:
    """Fetch and cache a remote actor profile."""
    if not _remote_url_allowed(actor_url):
        logger.warning(f"AP: отклонён небезопасный actor_url {actor_url}")
        return None
    conn = _get_db()
    existing = conn.execute(
        "SELECT * FROM ap_actors WHERE actor_url=? AND is_local=0", (actor_url,)
    ).fetchone()
    if existing:
        age = time.time() - existing["last_refreshed"]
        if age < 3600:  # Cache for 1 hour
            return dict(existing)
    try:
        resp = httpx.get(actor_url, headers={"Accept": "application/activity+json"}, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        username = data.get("preferredUsername", "")
        domain = urlparse(actor_url).netloc
        pubkey = ""
        if "publicKey" in data:
            pubkey = data["publicKey"].get("publicKeyPem", "")
        now = time.time()
        if existing:
            conn.execute("""
                UPDATE ap_actors SET domain=?, display_name=?, summary=?,
                avatar_url=?, header_url=?, public_key_pem=?, profile_data=?,
                inbox_url=?, shared_inbox_url=?, followers_url=?,
                following_url=?, outbox_url=?, last_refreshed=?, updated_at=?
                WHERE id=?
            """, (
                domain, data.get("name", ""), data.get("summary", ""),
                data.get("icon", {}).get("url", "") if isinstance(data.get("icon"), dict) else "",
                data.get("image", {}).get("url", "") if isinstance(data.get("image"), dict) else "",
                pubkey, json.dumps(data),
                data.get("inbox", ""), data.get("sharedInbox", ""),
                data.get("followers", ""), data.get("following", ""),
                data.get("outbox", ""), now, now, existing["id"]
            ))
        else:
            conn.execute("""
                INSERT INTO ap_actors (id, username, domain, actor_url, inbox_url,
                shared_inbox_url, followers_url, following_url, outbox_url,
                public_key_pem, display_name, summary, avatar_url, header_url,
                profile_data, is_local, last_refreshed, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                str(uuid.uuid4()), username, domain, actor_url,
                data.get("inbox", ""), data.get("sharedInbox", ""),
                data.get("followers", ""), data.get("following", ""),
                data.get("outbox", ""), pubkey,
                data.get("name", ""), data.get("summary", ""),
                data.get("icon", {}).get("url", "") if isinstance(data.get("icon"), dict) else "",
                data.get("image", {}).get("url", "") if isinstance(data.get("image"), dict) else "",
                json.dumps(data), 0, now, now, now
            ))
        conn.commit()
        row = conn.execute("SELECT * FROM ap_actors WHERE actor_url=?", (actor_url,)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"Failed to resolve remote actor {actor_url}: {e}")
        return dict(existing) if existing else None

# --- ActivityPub JSON-LD ---

CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    {"manuallyApprovesFollowers": "as:manuallyApprovesFollowers", "sensitive": "as:sensitive"}
]

def build_actor_json(actor: dict) -> dict:
    """Build JSON-LD Actor document."""
    pubkey_pem = actor.get("public_key_pem", "")
    actor_url = actor.get("actor_url", _actor_url(actor["username"]))
    return {
        "@context": CONTEXT,
        "id": actor_url,
        "type": "Person",
        "following": actor.get("following_url", _following_url(actor["username"])),
        "followers": actor.get("followers_url", _followers_url(actor["username"])),
        "inbox": actor.get("inbox_url", _inbox_url(actor["username"])),
        "outbox": actor.get("outbox_url", _outbox_url(actor["username"])),
        "preferredUsername": actor["username"],
        "name": actor.get("display_name", actor["username"]),
        "summary": actor.get("summary", ""),
        "url": actor_url,
        "manuallyApprovesFollowers": False,
        "sensitive": False,
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": pubkey_pem
        },
        "icon": {"type": "Image", "url": actor.get("avatar_url", "")} if actor.get("avatar_url") else None,
        "image": {"type": "Image", "url": actor.get("header_url", "")} if actor.get("header_url") else None,
        "published": datetime.fromtimestamp(actor.get("created_at", 0), tz=timezone.utc).isoformat()
    }

# --- HTTP Signatures ---

def _parse_signature_header(sig_header: str) -> dict:
    """Parse Signature header into dict."""
    result = {}
    for part in re.findall(r'(\w+)="([^"]*)"', sig_header):
        result[part[0]] = part[1]
    for part in re.findall(r'(\w+)="([^"]*)"', sig_header.replace('\\n', '\n')):
        result[part[0]] = result.get(part[0], part[1])
    return result

def _verify_http_signature(request: Request, body: bytes) -> bool:
    """Verify HTTP Signature from incoming request."""
    sig_header = request.headers.get("signature", "")
    if not sig_header:
        sig_header = request.headers.get("signature-input", "")
    if not sig_header:
        return False
    sig_parts = _parse_signature_header(sig_header)
    key_id = sig_parts.get("keyId", "")
    if not key_id:
        return False
    # Extract actor URL from keyId
    actor_url = key_id.replace("#main-key", "")
    actor = resolve_remote_actor(actor_url)
    if not actor:
        return False
    pubkey_pem = actor.get("public_key_pem", "")
    if not pubkey_pem:
        return False
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
        public_key = serialization.load_pem_public_key(pubkey_pem.encode(), backend=default_backend())
        # Build signed string
        headers_to_sign = sig_parts.get("headers", "date").split(" ")
        signed_string = ""
        for h in headers_to_sign:
            if h == "(request-target)":
                signed_string += f"(request-target): {request.method.lower()} {request.url.path}"
                if request.url.query:
                    signed_string += f"?{request.url.query}"
            elif h == "date":
                signed_string += f"date: {request.headers.get('date', '')}"
            elif h == "digest":
                signed_string += f"digest: {request.headers.get('digest', '')}"
            elif h == "content-type":
                signed_string += f"content-type: {request.headers.get('content-type', '')}"
            elif h == "host":
                signed_string += f"host: {request.headers.get('host', '')}"
            signed_string += "\n"
        signed_string = signed_string.rstrip("\n")
        signature = base64.b64decode(sig_parts.get("signature", ""))
        public_key.verify(signature, signed_string.encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception as e:
        logger.warning(f"Signature verification failed: {e}")
        return False

def _verify_inbox_request(request: Request, body: bytes, expected_actor: str = "") -> bool:
    """Полная проверка входящей AP-активности: подпись + digest + свежесть date."""
    sig_header = request.headers.get("signature", "") or request.headers.get("signature-input", "")
    if not sig_header:
        logger.warning("ActivityPub: отсутствует Signature header")
        return False
    sig_parts = _parse_signature_header(sig_header)
    digest_hdr = request.headers.get("digest", "")
    if digest_hdr:
        try:
            _, _, digest_b64 = digest_hdr.partition("=")
            if hashlib.sha256(body).digest() != base64.b64decode(digest_b64):
                logger.warning("ActivityPub: digest mismatch")
                return False
        except Exception as e:
            logger.warning(f"ActivityPub: digest check failed: {e}")
            return False
    date_hdr = request.headers.get("date", "")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt.timestamp() and abs(time.time() - dt.timestamp()) > 1800:
                logger.warning("ActivityPub: дата запроса вне окна 30 минут")
                return False
        except Exception:
            swallow.note('activitypub_module._verify_inbox_request:L471')
            return False
    if not _verify_http_signature(request, body):
        return False
    key_id = sig_parts.get("keyId", "")
    key_actor = key_id.replace("#main-key", "")
    if expected_actor and key_actor:
        if not (expected_actor.rstrip("/") == key_actor.rstrip("/")
                or expected_actor.rstrip("/").startswith(key_actor.rstrip("/") + "/")):
            logger.warning(f"ActivityPub: keyId {key_actor} не соответствует актору {expected_actor}")
            return False
    return True

# --- WebFinger ---

@router.get("/.well-known/webfinger", include_in_schema=False)
async def webfinger(resource: str = "", request: Request = None):
    """WebFinger endpoint for user discovery (@user@domain)."""
    if not resource:
        return JSONResponse({"error": "Missing resource"}, status_code=400)
    # Parse acct:user@domain
    match = re.match(r'acct:(.+)@(.+)', resource)
    if not match:
        return JSONResponse({"error": "Invalid resource format"}, status_code=400)
    username, req_domain = match.group(1), match.group(2)
    # Only handle our domain
    if req_domain != get_domain():
        return JSONResponse({"error": "Unknown domain"}, status_code=404)
    conn = _get_db()
    actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (username,)
    ).fetchone()
    if not actor:
        return JSONResponse({"error": "User not found"}, status_code=404)
    actor_url = _actor_url(username)
    return JSONResponse({
        "subject": f"acct:{username}@{get_domain()}",
        "aliases": [actor_url, f"{_base_url()}/social/profile?id={username}"],
        "links": [
            {"rel": "self", "type": "application/activity+json", "href": actor_url},
            {"rel": "http://webfinger.net/rels/profile-page", "type": "text/html", "href": f"{_base_url()}/social/profile?id={username}"}
        ]
    })

# --- ActivityPub Actor ---

@router.get("/api/social/ap/actors/{username}", include_in_schema=False)
async def ap_actor(username: str):
    conn = _get_db()
    actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (username,)
    ).fetchone()
    if not actor:
        return JSONResponse({"error": "Actor not found"}, status_code=404)
    return JSONResponse(
        build_actor_json(dict(actor)),
        media_type="application/activity+json"
    )

@router.get("/api/social/ap/actors/{username}/followers", include_in_schema=False)
async def ap_followers(username: str, page: Optional[str] = None):
    conn = _get_db()
    actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (username,)
    ).fetchone()
    if not actor:
        return JSONResponse({"error": "Not found"}, status_code=404)
    actor_url = _actor_url(username)
    followers = conn.execute(
        "SELECT a.* FROM ap_follows f JOIN ap_actors a ON f.follower_id=a.id "
        "WHERE f.followed_id=? AND f.state='accepted'", (actor["id"],)
    ).fetchall()
    items = [f["actor_url"] for f in followers]
    return JSONResponse({
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_url}/followers",
        "type": "OrderedCollection",
        "totalItems": len(items),
        "items": items
    })

@router.get("/api/social/ap/actors/{username}/following", include_in_schema=False)
async def ap_following(username: str):
    conn = _get_db()
    actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (username,)
    ).fetchone()
    if not actor:
        return JSONResponse({"error": "Not found"}, status_code=404)
    actor_url = _actor_url(username)
    following = conn.execute(
        "SELECT a.* FROM ap_follows f JOIN ap_actors a ON f.followed_id=a.id "
        "WHERE f.follower_id=? AND f.state='accepted'", (actor["id"],)
    ).fetchall()
    items = [f["actor_url"] for f in following]
    return JSONResponse({
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_url}/following",
        "type": "OrderedCollection",
        "totalItems": len(items),
        "items": items
    })

@router.get("/api/social/ap/actors/{username}/outbox", include_in_schema=False)
async def ap_outbox(username: str, page: Optional[str] = None):
    conn = _get_db()
    actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (username,)
    ).fetchone()
    if not actor:
        return JSONResponse({"error": "Not found"}, status_code=404)
    actor_url = _actor_url(username)
    # Get public posts from social_module
    posts = []
    if SOCIAL_MODULE:
        try:
            conn_social = SOCIAL_MODULE._get_db()
            rows = conn_social.execute(
                "SELECT p.*, u.display_name, u.avatar_path FROM social_posts p "
                "JOIN social_users u ON p.author_id = u.profile_id "
                "WHERE p.author_id=? AND p.privacy=0 ORDER BY p.created_at DESC LIMIT 20",
                (username,)
            ).fetchall()
            for r in rows:
                post_id = r["id"]
                post_actor = _actor_url(username)
                post_url = f"{_base_url()}/social/feed?post_id={post_id}"
                content = r.get("content", "")
                created = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).isoformat()
                posts.append({
                    "@context": "https://www.w3.org/ns/activitystreams",
                    "id": f"{post_url}#activity",
                    "type": "Create",
                    "actor": post_actor,
                    "published": created,
                    "to": ["https://www.w3.org/ns/activitystreams#Public"],
                    "cc": [f"{post_actor}/followers"],
                    "object": {
                        "id": post_url,
                        "type": "Note",
                        "published": created,
                        "attributedTo": post_actor,
                        "content": content,
                        "to": ["https://www.w3.org/ns/activitystreams#Public"],
                        "cc": [f"{post_actor}/followers"],
                        "sensitive": False
                    }
                })
        except Exception as e:
            logger.warning(f"Outbox error: {e}")
    return JSONResponse({
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_url}/outbox",
        "type": "OrderedCollection",
        "totalItems": len(posts),
        "orderedItems": posts
    })

# --- Own identity ---

@router.get("/api/social/ap/local-users", include_in_schema=False)
async def ap_local_users():
    """Список локальных (публичных) пользователей инстанса — для каталога пиров."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT username, display_name, actor_url, avatar_url FROM ap_actors WHERE is_local=1 ORDER BY display_name"
    ).fetchall()
    return JSONResponse([{
        "username": r["username"],
        "display_name": r["display_name"] or r["username"],
        "actor_url": r["actor_url"],
        "avatar_url": r["avatar_url"],
        "handle": f"{r['username']}@{get_domain()}"
    } for r in rows])

@router.get("/api/social/ap/self", include_in_schema=False)
async def ap_self(request: Request):
    """Информация о собственном ActivityPub-аккаунте текущего пользователя."""
    try:
        from social_module import _get_active_profile, _get_profile_name
    except Exception:
        swallow.note('activitypub_module.ap_self:L652')
        _get_active_profile = lambda req: "default"
        _get_profile_name = lambda pid: pid
    profile_id = _get_active_profile(request)
    display_name = _get_profile_name(profile_id)
    actor = get_or_create_local_actor(profile_id, display_name)
    return JSONResponse({
        "handle": f"{profile_id}@{get_domain()}",
        "profile_id": profile_id,
        "display_name": display_name,
        "actor_url": actor.get("actor_url"),
        "inbox_url": actor.get("inbox_url"),
        "followers_url": actor.get("followers_url"),
        "base_url": _base_url(),
        "domain": get_domain()
    })

# --- Inbox ---

@router.post("/api/social/ap/actors/{username}/inbox", include_in_schema=False)
async def ap_inbox(username: str, request: Request, background_tasks: BackgroundTasks):
    """Receive activities from other servers (Follow, Create, Like, Announce, etc.)"""
    # Verify it's a local actor
    conn = _get_db()
    actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (username,)
    ).fetchone()
    if not actor:
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        body = await request.body()
        data = json.loads(body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    actor_url = data.get("actor", "")
    if not _verify_inbox_request(request, body, expected_actor=actor_url):
        return JSONResponse({"error": "Signature verification failed"}, status_code=401)
    activity_type = data.get("type", "")
    activity_id = data.get("id", str(uuid.uuid4()))
    actor_url = data.get("actor", "")
    object_data = data.get("object", {})
    object_id = object_data if isinstance(object_data, str) else object_data.get("id", "")
    object_type = ""
    if isinstance(object_data, dict):
        object_type = object_data.get("type", "")
    # Deduplicate
    existing = conn.execute(
        "SELECT id FROM ap_inbox WHERE activity_id=?", (activity_id,)
    ).fetchone()
    if existing:
        return JSONResponse({"ok": True})  # Already processed
    # Resolve remote actor
    remote_actor = resolve_remote_actor(actor_url)
    if not remote_actor:
        logger.warning(f"Could not resolve remote actor: {actor_url}")
        return JSONResponse({"ok": True})  # Accept anyway
    # Store in inbox
    now = time.time()
    conn.execute("""
        INSERT INTO ap_inbox (id, actor_id, activity_id, type, object_type, object_id, raw_json, created_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (str(uuid.uuid4()), remote_actor["id"], activity_id, activity_type, object_type, object_id, json.dumps(data), now))
    conn.commit()
    # Process activity
    background_tasks.add_task(_process_inbox_activity, activity_id)
    return JSONResponse({"ok": True})

@router.post("/api/social/ap/inbox", include_in_schema=False)
async def ap_shared_inbox(request: Request, background_tasks: BackgroundTasks):
    """Shared inbox вЂ” receive activities for any local user."""
    try:
        body = await request.body()
        data = json.loads(body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    # Determine the recipient from the activity
    to = data.get("to", [])
    if isinstance(to, str): to = [to]
    cc = data.get("cc", [])
    if isinstance(cc, str): cc = [cc]
    all_recipients = to + cc
    # Find our local actor
    conn = _get_db()
    local_actors = conn.execute(
        "SELECT username FROM ap_actors WHERE is_local=1"
    ).fetchall()
    for local in local_actors:
        local_actor_url = _actor_url(local["username"])
        local_followers_url = _followers_url(local["username"])
        # This activity is relevant to this local user
        if local_actor_url in all_recipients or local_followers_url in all_recipients or not all_recipients:
            # Re-route to their inbox
            return await ap_inbox(local["username"], request, background_tasks)
    return JSONResponse({"ok": True})

def _download_remote_media(attachments) -> str:
    # URL приходит из федеративного объекта: не тянем loopback/LAN (SSRF).
    """Скачать медиа из входящей AP-активности, вернуть JSON-массив для social_posts.attachments."""
    if not attachments:
        return "[]"
    if isinstance(attachments, dict):
        attachments = [attachments]
    if not isinstance(attachments, list):
        return "[]"
    result = []
    media_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "social", "post_media")
    try:
        os.makedirs(media_dir, exist_ok=True)
    except Exception:
        swallow.note('activitypub_module._download_remote_media:L761')
        media_dir = ""
    allowed_ext = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm', '.mov', '.ogg'}
    for att in attachments[:4]:
        url = ""
        if isinstance(att, dict):
            mt = att.get("mediaType", "")
            if att.get("url") and isinstance(att["url"], str):
                url = att["url"]
            elif isinstance(att.get("url"), dict):
                url = att["url"].get("href", "")
        elif isinstance(att, str):
            url = att
        if not url or not url.startswith(("http://", "https://")):
            continue
        if not _remote_url_allowed(url):
            continue
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext not in allowed_ext:
            ext = ".jpg" if "image" in mt else ".mp4" if "video" in mt else ""
            if not ext:
                continue
        try:
            with httpx.Client(timeout=15.0, verify=_AP_VERIFY, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.content
            if len(data) > 15 * 1024 * 1024:
                continue
            head = data[:512].lower()
            if b"<script" in head or b"<html" in head or b"<!doctype" in head:
                continue
            fname = f"{uuid.uuid4().hex}{ext}"
            if media_dir:
                with open(os.path.join(media_dir, fname), "wb") as f:
                    f.write(data)
                att_type = ("image" if "image" in mt else "video" if "video" in mt else "file")
                result.append({"url": f"/api/social/file/post_media/{fname}", "type": att_type})
        except Exception as e:
            logger.warning(f"Media download failed for {url}: {e}")
    return json.dumps(result, ensure_ascii=False)

def _save_remote_note(conn_s, remote_actor: dict, object_data, published: float = None) -> str:
    """Сохранить входящую Note как социальный пост (с медиа), вернуть post_id."""
    content = object_data.get("content", "") if isinstance(object_data, dict) else ""
    content = re.sub(r'<[^>]+>', '', content or "").strip()
    if not content:
        return ""
    now = time.time()
    if not published:
        try:
            published = datetime.fromisoformat(str(object_data.get("published", "")).replace("Z", "+00:00")).timestamp()
        except Exception:
            swallow.note('activitypub_module._save_remote_note:L815')
            published = now
    remote_uname = remote_actor.get("username", "remote")
    remote_dom = remote_actor.get("domain", "")
    remote_pid = f"{remote_uname}@{remote_dom}" if remote_dom else remote_uname
    remote_actor_url = remote_actor.get("actor_url", "")
    conn_s.execute(
        "INSERT INTO social_users (profile_id, display_name, avatar_path, joined_at, is_federated, federation_url, ap_actor_url, ap_remote_username, ap_remote_domain) VALUES (?,?,?,?,1,?,?,?,?) "
        "ON CONFLICT(profile_id) DO UPDATE SET display_name=excluded.display_name, "
        "avatar_path=CASE WHEN excluded.avatar_path<>'' THEN excluded.avatar_path ELSE social_users.avatar_path END, "
        "is_federated=1, ap_actor_url=excluded.ap_actor_url, ap_remote_username=excluded.ap_remote_username, ap_remote_domain=excluded.ap_remote_domain",
        (remote_pid,
         remote_actor.get("display_name") or remote_uname,
         remote_actor.get("avatar_url", "") or "",
         now,
         remote_actor_url,
         remote_uname, remote_dom)
    )
    post_id = str(object_data.get("id", "")).replace("/", "_").replace(":", "_")
    if not post_id:
        return ""
    attachments = _download_remote_media(object_data.get("attachment") or [])
    conn_s.execute(
        "INSERT OR IGNORE INTO social_posts (id, author_id, content, created_at, privacy, attachments, federation_id) VALUES (?,?,?,?,?,?,?)",
        (post_id, remote_pid, content, published, 0, attachments, object_data.get("id", ""))
    )
    conn_s.commit()
    followers = conn_s.execute(
        "SELECT profile_id FROM social_remote_follows WHERE actor_url=?",
        (remote_actor_url,)
    ).fetchall()
    for f in followers:
        conn_s.execute(
            "INSERT OR IGNORE INTO social_feeds (user_id, post_id, created_at) VALUES (?,?,?)",
            (f["profile_id"], post_id, published)
        )
    conn_s.commit()
    return post_id

def _backfill_remote_actor(remote_actor_url: str, limit: int = 40):
    if not _remote_url_allowed(remote_actor_url):
        logger.warning(f"AP backfill: отклонён небезопасный URL {remote_actor_url}")
        return
    """Backfill: подтянуть недавние посты удалённого актора в ленты локальных подписчиков."""
    try:
        from urllib.parse import urlencode
        actor = resolve_remote_actor(remote_actor_url)
        if not actor or not actor.get("outbox_url"):
            return 0
        outbox_url = actor["outbox_url"]
        if not _remote_url_allowed(outbox_url):
            logger.warning(f"AP backfill: отклонён небезопасный outbox {outbox_url}")
            return 0
        sep = "&" if "?" in outbox_url else "?"
        page_url = f"{outbox_url}{sep}page=true&limit={limit}"
        conn = _get_db()
        local_actor = conn.execute(
            "SELECT * FROM ap_actors WHERE is_local=1 LIMIT 1"
        ).fetchone()
        headers = {"Accept": "application/activity+json, application/ld+json; profile=\"https://www.w3.org/ns/activitystreams\""}
        with httpx.Client(timeout=20.0, verify=_AP_VERIFY, follow_redirects=True) as client:
            resp = client.get(page_url, headers=headers)
            if resp.status_code != 200:
                resp = client.get(outbox_url, headers=headers)
            data = resp.json()
        items = []
        if isinstance(data, dict):
            if isinstance(data.get("orderedItems"), list):
                items = data["orderedItems"]
            elif isinstance(data.get("first"), dict) and isinstance(data["first"].get("orderedItems"), list):
                items = data["first"]["orderedItems"]
            elif isinstance(data.get("first"), str):
                if not _remote_url_allowed(data["first"]):
                    items = []
                else:
                    with httpx.Client(timeout=20.0, verify=_AP_VERIFY) as c2:
                        items = c2.get(data["first"], headers=headers).json().get("orderedItems", [])
        if SOCIAL_MODULE:
            conn_s = SOCIAL_MODULE._get_db()
            saved = 0
            seen = set()
            for item in items[:limit]:
                if not isinstance(item, dict) or item.get("type") not in ("Create", "Announce"):
                    continue
                obj = item.get("object")
                actor_url = item.get("actor") or remote_actor_url
                if not isinstance(obj, dict):
                    continue
                if item.get("type") == "Announce":
                    obj = obj.get("object")
                    if isinstance(obj, str):
                        continue
                    if not isinstance(obj, dict):
                        continue
                note_id = obj.get("id", "")
                if not note_id or note_id in seen:
                    continue
                seen.add(note_id)
                act = resolve_remote_actor(actor_url) if actor_url != remote_actor_url else actor
                if act and _save_remote_note(conn_s, act, obj):
                    saved += 1
            return saved
    except Exception as e:
        logger.warning(f"Backfill error for {remote_actor_url}: {e}")
    return 0

async def _process_inbox_activity(activity_id: str):
    """Process an inbox activity."""
    conn = _get_db()
    row = conn.execute("SELECT * FROM ap_inbox WHERE id=?", (activity_id,)).fetchone()
    if not row or row["is_processed"]:
        return
    data = json.loads(row["raw_json"])
    activity_type = row["type"]
    remote_actor = dict(conn.execute("SELECT * FROM ap_actors WHERE id=?", (row["actor_id"],)).fetchone() or {})
    if not remote_actor:
        return
    remote_actor_url = remote_actor.get("actor_url", "")
    object_data = data.get("object", {})
    object_id = object_data if isinstance(object_data, str) else object_data.get("id", "")
    object_type = ""
    if isinstance(object_data, dict):
        object_type = object_data.get("type", "")
    conn = _get_db()  # Re-get in case of thread issues

    if activity_type == "Follow":
        # Someone wants to follow our user
        followed_username = None
        # Find the local actor being followed
        local_actors = conn.execute(
            "SELECT * FROM ap_actors WHERE is_local=1"
        ).fetchall()
        for la in local_actors:
            if la["actor_url"] == object_id or la["id"] == str(object_id) if isinstance(object_id, str) else False:
                followed_username = la["username"]
                break
        if not followed_username:
            logger.warning(f"Follow target not found: {object_id}")
            conn.execute("UPDATE ap_inbox SET is_processed=1 WHERE id=?", (row["id"],))
            conn.commit()
            return
        # Create follow relationship
        local_actor = conn.execute(
            "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (followed_username,)
        ).fetchone()
        if not local_actor:
            conn.execute("UPDATE ap_inbox SET is_processed=1 WHERE id=?", (row["id"],))
            conn.commit()
            return
        existing_follow = conn.execute(
            "SELECT * FROM ap_follows WHERE follower_id=? AND followed_id=?",
            (remote_actor["id"], local_actor["id"])
        ).fetchone()
        if existing_follow:
            conn.execute("UPDATE ap_follows SET state='accepted' WHERE id=?", (existing_follow["id"],))
        else:
            conn.execute("""
                INSERT INTO ap_follows (id, follower_id, followed_id, state, remote_activity_id, created_at)
                VALUES (?,?,?,'accepted',?,?)
            """, (str(uuid.uuid4()), remote_actor["id"], local_actor["id"], activity_id, time.time()))
        conn.commit()
        # Send Accept activity
        accept_id = str(uuid.uuid4())
        accept = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{_actor_url(followed_username)}#accepts/{accept_id}",
            "type": "Accept",
            "actor": _actor_url(followed_username),
            "object": {
                "id": activity_id or data.get("id", ""),
                "type": "Follow",
                "actor": remote_actor_url,
                "object": _actor_url(followed_username)
            }
        }
        _enqueue_delivery(local_actor["id"], remote_actor.get("inbox_url", ""), json.dumps(accept))
        # Also try shared inbox
        if remote_actor.get("shared_inbox_url"):
            _enqueue_delivery(local_actor["id"], remote_actor["shared_inbox_url"], json.dumps(accept))
        # Notify social module
        if SOCIAL_MODULE:
            try:
                conn_s = SOCIAL_MODULE._get_db()
                SOCIAL_MODULE._create_notification(
                    conn_s, followed_username, "follow", remote_actor.get("username", "remote"),
                    "profile", remote_actor.get("username", "")
                )
            except Exception as e:
                logger.warning(f"Follow notification error: {e}")

    elif activity_type == "Create" and object_type == "Note":
        # Remote user created a post
        if SOCIAL_MODULE:
            try:
                conn_s = SOCIAL_MODULE._get_db()
                try:
                    published = datetime.fromisoformat(str(object_data.get("published", "")).replace("Z", "+00:00")).timestamp() if isinstance(object_data, dict) else time.time()
                except Exception:
                    swallow.note('activitypub_module._process_inbox_activity:L1013')
                    published = time.time()
                _save_remote_note(conn_s, remote_actor, object_data, published)
            except Exception as e:
                logger.warning(f"Create Note error: {e}")

    elif activity_type == "Like":
        obj_id = object_id if isinstance(object_data, str) else (object_data.get("id", "") if isinstance(object_data, dict) else "")
        if SOCIAL_MODULE and obj_id:
            try:
                conn_s = SOCIAL_MODULE._get_db()
                post_id = str(obj_id).replace("/", "_").replace(":", "_")
                post = conn_s.execute("SELECT likes_count FROM social_posts WHERE id=?", (post_id,)).fetchone()
                if post:
                    conn_s.execute("UPDATE social_posts SET likes_count=likes_count+1 WHERE id=?", (post_id,))
                    conn_s.commit()
                    logger.info(f"Like on {post_id} from {remote_actor.get('actor_url','')}")
            except Exception as e:
                logger.warning(f"Like error: {e}")

    elif activity_type == "Announce":
        if SOCIAL_MODULE:
            try:
                conn_s = SOCIAL_MODULE._get_db()
                obj = object_data if isinstance(object_data, dict) else {}
                inner = obj.get("object") if isinstance(obj, dict) else None
                if isinstance(inner, str):
                    inner = None
                if isinstance(inner, dict):
                    target_url = inner.get("id") or obj.get("id")
                    if target_url and _remote_url_allowed(str(target_url)):
                        # fetch the announced note if we don't have it
                        post_id = str(target_url).replace("/", "_").replace(":", "_")
                        exists = conn_s.execute("SELECT 1 FROM social_posts WHERE id=?", (post_id,)).fetchone()
                        if not exists:
                            try:
                                with httpx.Client(timeout=15.0, verify=_AP_VERIFY, follow_redirects=True) as c:
                                    r = c.get(target_url, headers={"Accept": "application/activity+json"})
                                if r.status_code == 200:
                                    note = r.json()
                                    _save_remote_note(conn_s, remote_actor, note)
                            except Exception as e:
                                logger.warning(f"Announce fetch error: {e}")
                        feed_rows = conn_s.execute(
                            "SELECT profile_id FROM social_remote_follows WHERE actor_url=?",
                            (remote_actor.get("actor_url", ""),)
                        ).fetchall()
                        for f in feed_rows:
                            conn_s.execute(
                                "INSERT OR IGNORE INTO social_feeds (user_id, post_id, created_at) VALUES (?,?,?)",
                                (f["profile_id"], post_id, time.time())
                            )
                        conn_s.commit()
            except Exception as e:
                logger.warning(f"Announce error: {e}")

    elif activity_type == "Accept":
        obj = data.get("object", {})
        follow_activity_id = obj.get("id", "") if isinstance(obj, dict) else ""
        if follow_activity_id:
            # Our outgoing Follow was accepted -> mark accepted + backfill
            row = conn.execute(
                "SELECT * FROM ap_follows WHERE remote_activity_id=?", (follow_activity_id,)
            ).fetchone()
            if row:
                conn.execute("UPDATE ap_follows SET state='accepted' WHERE id=?", (row["id"],))
                conn.commit()
                followed = conn.execute("SELECT * FROM ap_actors WHERE id=?", (row["followed_id"],)).fetchone()
                if followed:
                    actor_url = followed.get("actor_url", "")
                    if SOCIAL_MODULE:
                        try:
                            conn_s = SOCIAL_MODULE._get_db()
                            conn_s.execute(
                                "UPDATE social_remote_follows SET status='accepted' WHERE actor_url=?",
                                (actor_url,)
                            )
                            conn_s.commit()
                        except Exception:
                            swallow.note('activitypub_module._process_inbox_activity:L1092')
                            pass
                    _backfill_remote_actor(actor_url)

    elif activity_type == "Delete":
        # Remove remote post
        if SOCIAL_MODULE:
            try:
                conn_s = SOCIAL_MODULE._get_db()
                obj_id = object_id.replace("/", "_").replace(":", "_")
                conn_s.execute("DELETE FROM social_posts WHERE id=?", (obj_id,))
                conn_s.commit()
            except Exception:
                swallow.note('activitypub_module._process_inbox_activity:L1105')
                pass

    elif activity_type == "Undo":
        # Undo a previous activity (Follow / Like / Announce / Create)
        obj = data.get("object", {})
        if isinstance(obj, str):
            obj = {"id": obj}
        if isinstance(obj, dict):
            undo_type = obj.get("type", "")
            if undo_type == "Follow":
                undo_follow_activity_id = obj.get("id", "")
                conn.execute(
                    "UPDATE ap_follows SET state='rejected' WHERE remote_activity_id=?",
                    (undo_follow_activity_id,)
                )
                conn.commit()
            elif undo_type == "Like":
                like_target = obj.get("object", "")
                if SOCIAL_MODULE and like_target:
                    try:
                        conn_s = SOCIAL_MODULE._get_db()
                        post_id = str(like_target).replace("/", "_").replace(":", "_")
                        conn_s.execute("UPDATE social_posts SET likes_count=MAX(0, likes_count-1) WHERE id=?", (post_id,))
                        conn_s.commit()
                    except Exception:
                        swallow.note('activitypub_module._process_inbox_activity:L1131')
                        pass
            elif undo_type == "Announce":
                ann_target = obj.get("object", "")
                if SOCIAL_MODULE and ann_target:
                    try:
                        conn_s = SOCIAL_MODULE._get_db()
                        post_id = str(ann_target).replace("/", "_").replace(":", "_")
                        conn_s.execute(
                            "DELETE FROM social_feeds WHERE post_id=? AND user_id IN "
                            "(SELECT profile_id FROM social_remote_follows WHERE actor_url=?)",
                            (post_id, remote_actor.get("actor_url", ""))
                        )
                        conn_s.commit()
                    except Exception:
                        swallow.note('activitypub_module._process_inbox_activity:L1146')
                        pass

    conn.execute("UPDATE ap_inbox SET is_processed=1 WHERE id=?", (row["id"],))
    conn.commit()

# --- Delivery ---

def _enqueue_delivery(actor_id: str, inbox_url: str, activity_json: str, activity_id: str = ""):
    """Add an activity to the delivery queue."""
    conn = _get_db()
    try:
        data = json.loads(activity_json)
        act_id = activity_id or data.get("id", str(uuid.uuid4()))
        conn.execute("""
            INSERT INTO ap_delivery_queue (id, actor_id, inbox_url, activity_json, activity_id, state, created_at)
            VALUES (?,?,?,?,?,'pending',?)
        """, (str(uuid.uuid4()), actor_id, inbox_url, activity_json, act_id, time.time()))
        conn.commit()
    except Exception as e:
        logger.warning(f"Enqueue error: {e}")

def _sign_and_send(actor_id: str, inbox_url: str, activity_json: str) -> bool:
    if not _remote_url_allowed(inbox_url):
        logger.warning(f"AP: отклонён небезопасный inbox_url {inbox_url}")
        return False
    """Sign an outgoing activity and POST to the remote inbox."""
    conn = _get_db()
    actor = conn.execute("SELECT * FROM ap_actors WHERE id=?", (actor_id,)).fetchone()
    if not actor:
        return False
    private_key_pem = actor.get("private_key_pem", "")
    if not private_key_pem:
        logger.warning(f"No private key for actor {actor_id}")
        return False
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None, backend=default_backend()
        )
        parsed = urlparse(inbox_url)
        path = parsed.path
        if parsed.query: path += f"?{parsed.query}"
        host = parsed.netloc
        digest = base64.b64encode(hashlib.sha256(activity_json.encode()).digest()).decode()
        date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        signed_string = f"(request-target): post {path}\nhost: {host}\ndate: {date}\ndigest: SHA-256={digest}"
        signature = base64.b64encode(
            private_key.sign(signed_string.encode(), padding.PKCS1v15(), hashes.SHA256())
        ).decode()
        key_id = _key_id(actor["actor_url"])
        sig_header = f'keyId="{key_id}",algorithm="rsa-sha256",headers="(request-target) host date digest",signature="{signature}"'
        headers = {
            "Content-Type": "application/activity+json",
            "Host": host,
            "Date": date,
            "Digest": f"SHA-256={digest}",
            "Signature": sig_header,
            "User-Agent": f"InfinitySocial/1.0 (ActivityPub; {_base_url()})"
        }
        resp = httpx.post(inbox_url, content=activity_json, headers=headers, timeout=15)
        if resp.status_code in (200, 201, 202, 204):
            return True
        else:
            logger.warning(f"Delivery to {inbox_url} failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Delivery error to {inbox_url}: {e}")
        return False

def _process_delivery_queue():
    """Background worker to process pending deliveries."""
    while True:
        try:
            conn = _get_db()
            pending = conn.execute(
                "SELECT * FROM ap_delivery_queue WHERE state='pending' AND retries < 5 ORDER BY created_at ASC LIMIT 10"
            ).fetchall()
            for item in pending:
                success = _sign_and_send(item["actor_id"], item["inbox_url"], item["activity_json"])
                if success:
                    conn.execute("UPDATE ap_delivery_queue SET state='sent', retries=retries+1 WHERE id=?", (item["id"],))
                else:
                    conn.execute("UPDATE ap_delivery_queue SET state='failed', retries=retries+1, last_error='Delivery failed' WHERE id=?", (item["id"],))
                conn.commit()
        except Exception as e:
            logger.warning(f"Delivery queue error: {e}")
        threading.Event().wait(30)  # Check every 30 seconds

# --- Federation helpers (called by social_module) ---

def federate_post(profile_id: str, post_id: str, content: str):
    """Federate a new post to all followers."""
    conn = _get_db()
    local_actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (profile_id,)
    ).fetchone()
    if not local_actor:
        # No ActivityPub actor for this user
        return
    actor_url = _actor_url(profile_id)
    post_url = f"{_base_url()}/social/feed?post_id={post_id}"
    now = datetime.now(timezone.utc).isoformat()
    # Create activity
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{post_url}#activity",
        "type": "Create",
        "actor": actor_url,
        "published": now,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{actor_url}/followers"],
        "object": {
            "id": post_url,
            "type": "Note",
            "published": now,
            "attributedTo": actor_url,
            "content": content,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [f"{actor_url}/followers"],
            "sensitive": False
        }
    }
    activity_json = json.dumps(activity)
    # Deliver to all followers
    followers = conn.execute(
        "SELECT a.* FROM ap_follows f JOIN ap_actors a ON f.follower_id=a.id "
        "WHERE f.followed_id=? AND f.state='accepted'", (local_actor["id"],)
    ).fetchall()
    for follower in followers:
        inbox = follower.get("inbox_url", "")
        if inbox:
            _enqueue_delivery(local_actor["id"], inbox, activity_json, activity.get("id", ""))
        if follower.get("shared_inbox_url") and follower["shared_inbox_url"] != inbox:
            _enqueue_delivery(local_actor["id"], follower["shared_inbox_url"], activity_json, activity.get("id", ""))

def federate_follow(local_username: str, remote_actor_url: str) -> bool:
    """Send a Follow activity to a remote user."""
    conn = _get_db()
    local_actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (local_username,)
    ).fetchone()
    if not local_actor:
        return False
    remote_actor = resolve_remote_actor(remote_actor_url)
    if not remote_actor:
        return False
    remote_inbox = remote_actor.get("inbox_url", "")
    if not remote_inbox:
        return False
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{_actor_url(local_username)}#follows/{str(uuid.uuid4())}",
        "type": "Follow",
        "actor": _actor_url(local_username),
        "object": remote_actor_url
    }
    activity_json = json.dumps(activity)
    _enqueue_delivery(local_actor["id"], remote_inbox, activity_json, activity["id"])
    # Also try shared inbox
    if remote_actor.get("shared_inbox_url") and remote_actor["shared_inbox_url"] != remote_inbox:
        _enqueue_delivery(local_actor["id"], remote_actor["shared_inbox_url"], activity_json, activity["id"])
    # Create pending follow
    existing = conn.execute(
        "SELECT * FROM ap_follows WHERE follower_id=? AND followed_id=?",
        (local_actor["id"], remote_actor["id"])
    ).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO ap_follows (id, follower_id, followed_id, state, remote_activity_id, created_at)
            VALUES (?,?,?,'pending',?,?)
        """, (str(uuid.uuid4()), local_actor["id"], remote_actor["id"], activity["id"], time.time()))
        conn.commit()
    return True

def federate_unfollow(local_username: str, remote_actor_url: str) -> bool:
    """Send Undo(Follow) to a remote user."""
    conn = _get_db()
    local_actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (local_username,)
    ).fetchone()
    if not local_actor:
        return False
    remote = resolve_remote_actor(remote_actor_url)
    if not remote:
        return False
    inbox = remote.get("inbox_url", "")
    if not inbox:
        return False
    follow = conn.execute(
        "SELECT * FROM ap_follows WHERE follower_id=? AND followed_id=? ORDER BY created_at DESC LIMIT 1",
        (local_actor["id"], remote["id"])
    ).fetchone()
    follow_activity_id = follow["remote_activity_id"] if follow else f"{_actor_url(local_username)}#follows/{str(uuid.uuid4())}"
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{_actor_url(local_username)}#undo/{str(uuid.uuid4())}",
        "type": "Undo",
        "actor": _actor_url(local_username),
        "object": {
            "id": follow_activity_id,
            "type": "Follow",
            "actor": _actor_url(local_username),
            "object": remote_actor_url
        }
    }
    activity_json = json.dumps(activity)
    _enqueue_delivery(local_actor["id"], inbox, activity_json, activity["id"])
    if remote.get("shared_inbox_url") and remote["shared_inbox_url"] != inbox:
        _enqueue_delivery(local_actor["id"], remote["shared_inbox_url"], activity_json, activity["id"])
    if follow:
        conn.execute("UPDATE ap_follows SET state='rejected' WHERE id=?", (follow["id"],))
        conn.commit()
    return True

def _federate_like(local_username: str, remote_actor_url: str, object_url: str, like: bool = True,
                   published: str = "") -> bool:
    """Send Like / Undo(Like) to the remote author of a post."""
    conn = _get_db()
    local_actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (local_username,)
    ).fetchone()
    if not local_actor:
        return False
    remote = resolve_remote_actor(remote_actor_url)
    if not remote:
        return False
    inbox = remote.get("inbox_url", "")
    if not inbox:
        return False
    if like:
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{_actor_url(local_username)}#likes/{str(uuid.uuid4())}",
            "type": "Like",
            "actor": _actor_url(local_username),
            "object": object_url,
            "published": published
        }
    else:
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{_actor_url(local_username)}#undolike/{str(uuid.uuid4())}",
            "type": "Undo",
            "actor": _actor_url(local_username),
            "object": {
                "id": f"{_actor_url(local_username)}#likes/{str(uuid.uuid4())}",
                "type": "Like",
                "actor": _actor_url(local_username),
                "object": object_url
            }
        }
    activity_json = json.dumps(activity)
    _enqueue_delivery(local_actor["id"], inbox, activity_json, activity["id"])
    if remote.get("shared_inbox_url") and remote["shared_inbox_url"] != inbox:
        _enqueue_delivery(local_actor["id"], remote["shared_inbox_url"], activity_json, activity["id"])
    return True

def federate_like(profile_id: str, post_id: str, like: bool = True) -> bool:
    """Federate Like/Undo(Like) for a social post if its author is remote."""
    try:
        conn = _get_db()
        conn_s = SOCIAL_MODULE._get_db()
        post = conn_s.execute(
            "SELECT p.federation_id, u.ap_actor_url, u.ap_remote_username FROM social_posts p "
            "JOIN social_users u ON p.author_id=u.profile_id WHERE p.id=?",
            (post_id,)
        ).fetchone()
        if not post or not post["ap_actor_url"] or not post["federation_id"]:
            return False
        now = datetime.now(timezone.utc).isoformat()
        return _federate_like(profile_id, post["ap_actor_url"], post["federation_id"], like, now)
    except Exception as e:
        logger.warning(f"federate_like error: {e}")
        return False

def federate_announce(profile_id: str, post_id: str) -> bool:
    """Send Announce (boost) of a federated post to its remote author."""
    try:
        conn = _get_db()
        conn_s = SOCIAL_MODULE._get_db()
        post = conn_s.execute(
            "SELECT p.federation_id, u.ap_actor_url FROM social_posts p "
            "JOIN social_users u ON p.author_id=u.profile_id WHERE p.id=?",
            (post_id,)
        ).fetchone()
        if not post or not post["ap_actor_url"] or not post["federation_id"]:
            return False
        local_actor = conn.execute(
            "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (profile_id,)
        ).fetchone()
        remote = resolve_remote_actor(post["ap_actor_url"])
        if not local_actor or not remote or not remote.get("inbox_url"):
            return False
        now = datetime.now(timezone.utc).isoformat()
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{_actor_url(profile_id)}#announce/{str(uuid.uuid4())}",
            "type": "Announce",
            "actor": _actor_url(profile_id),
            "object": post["federation_id"],
            "published": now,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [f"{_actor_url(profile_id)}/followers"]
        }
        activity_json = json.dumps(activity)
        _enqueue_delivery(local_actor["id"], remote["inbox_url"], activity_json, activity["id"])
        if remote.get("shared_inbox_url") and remote["shared_inbox_url"] != remote["inbox_url"]:
            _enqueue_delivery(local_actor["id"], remote["shared_inbox_url"], activity_json, activity["id"])
        return True
    except Exception as e:
        logger.warning(f"federate_announce error: {e}")
        return False

def federate_delete_post(profile_id: str, post_url: str):
    """Send Delete activity when a post is deleted."""
    conn = _get_db()
    local_actor = conn.execute(
        "SELECT * FROM ap_actors WHERE username=? AND is_local=1", (profile_id,)
    ).fetchone()
    if not local_actor:
        return
    actor_url = _actor_url(profile_id)
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{post_url}#delete/{str(uuid.uuid4())}",
        "type": "Delete",
        "actor": actor_url,
        "object": {"id": post_url, "type": "Note"}
    }
    activity_json = json.dumps(activity)
    followers = conn.execute(
        "SELECT a.* FROM ap_follows f JOIN ap_actors a ON f.follower_id=a.id "
        "WHERE f.followed_id=? AND f.state='accepted'", (local_actor["id"],)
    ).fetchall()
    for follower in followers:
        inbox = follower.get("inbox_url", "")
        if inbox:
            _enqueue_delivery(local_actor["id"], inbox, activity_json, activity.get("id", ""))
        if follower.get("shared_inbox_url") and follower["shared_inbox_url"] != inbox:
            _enqueue_delivery(local_actor["id"], follower["shared_inbox_url"], activity_json, activity.get("id", ""))

# --- Public endpoints for debugging ---

@router.get("/api/social/ap/self", include_in_schema=False)
async def ap_self():
    """Открытый каталог-эндпоинт: данные инстанса для обнаружения другими серверами."""
    conn = _get_db()
    local = conn.execute("SELECT username, actor_url, public_key_pem FROM ap_actors WHERE is_local=1").fetchall()
    base = _base_url()
    first = local[0] if local else None
    return JSONResponse({
        "domain": urlparse(base).netloc,
        "base_url": base,
        "software": {"name": "infinitysocial", "version": "1.0.0"},
        "protocols": ["activitypub"],
        "capabilities": ["webfinger", "nodeinfo", "gossip", "pairing"],
        "actor_base": f"{base}/api/social/ap/actors",
        "shared_inbox": _shared_inbox_url(),
        "local_users": len(local),
        "public_actor": first["actor_url"] if first else "",
        "public_key": first["public_key_pem"] if first else "",
    })

@router.get("/api/social/ap/nodeinfo", include_in_schema=False)
async def nodeinfo():
    return JSONResponse({
        "version": "2.0",
        "software": {"name": "infinitysocial", "version": "1.0.0"},
        "protocols": ["activitypub"],
        "services": {"inbound": [], "outbound": ["atom1.0", "rss2.0"]},
        "openRegistrations": True,
        "usage": {"users": {"total": 0}},
        "metadata": {}
    })

@router.get("/.well-known/nodeinfo", include_in_schema=False)
async def well_known_nodeinfo():
    return JSONResponse({
        "links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0", "href": f"{_base_url()}/api/social/ap/nodeinfo"}]
    })

# Start delivery worker in background thread
def start_delivery_worker():
    t = threading.Thread(target=_process_delivery_queue, daemon=True)
    t.start()

start_delivery_worker()
