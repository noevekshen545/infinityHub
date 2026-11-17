"""legacy_media_player — секция main.py, вынесенная без изменения логики.

Маршруты перенесены механически; include_router вызывается в main.py
на прежнем месте секции, поэтому порядок регистрации не изменился.
"""

from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from pathlib import Path
from urllib.parse import quote
from urllib.parse import unquote
import asyncio
import base64
import glob
import hashlib
import httpx
import io
import json
import mimetypes
import os
import re
import server
import shutil
import subprocess
import sys
import threading
import time
import transcoder
import worker_pool
from fastapi import APIRouter, Request
from routes.legacy_common import _aio_read, _cache_invalidate, _get_cached_music_index, _get_cached_video_index, _run_sync, _stream_file_range, logger
import swallow

# секция читает файлы из каталога приложения (app/), а не из routes/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Флаг модуля для новых вкладок рекомендаций (фильмы и сериалы).
RECOMMEND_MODULE = "media_recommend"

router = APIRouter()


@router.get("/api/media/music-index")
async def api_media_music_index(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    q = request.query_params
    try:
        limit = int(q.get("limit", 100))
        offset = int(q.get("offset", 0))
    except ValueError:
        swallow.note('legacy_media_player.api_media_music_index:L48')
        limit, offset = 100, 0
    genre = q.get("genre") or None
    mood = q.get("mood") or None
    pid = _profile_id_of(request)
    if mood:
        import module_flags
        if not module_flags.enabled_for("media_mood", pid or ""):
            mood = None
    if genre or mood or limit > 0:
        return await worker_pool.run_async(server.get_music_index, limit=limit,
                                           offset=offset, genre=genre, profile_id=pid,
                                           mood=mood)
    return await _get_cached_music_index(pid)

@router.get("/api/media/video-index")
async def api_media_video_index(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    return await _get_cached_video_index(_profile_id_of(request))

@router.get("/api/media/search")
async def api_media_search(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    query = request.query_params.get("q", "")
    index = await _get_cached_music_index(_profile_id_of(request))
    results = await worker_pool.run_async(server.search_tracks, query, index.get('tracks', []))
    return {"results": results}

@router.get("/api/media/tracks-by-artist")
async def api_media_tracks_by_artist(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    artist = request.query_params.get("artist", "")
    index = await _get_cached_music_index(_profile_id_of(request))
    tracks = [t for t in index.get('tracks', []) if t.get('artist') == artist]
    return {"tracks": tracks}

@router.get("/api/media/tracks-by-album")
async def api_media_tracks_by_album(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    album = request.query_params.get("album", "")
    index = await _get_cached_music_index(_profile_id_of(request))
    tracks = [t for t in index.get('tracks', []) if t.get('album') == album]
    return {"tracks": tracks}

@router.get("/api/media/lyrics")
async def api_media_lyrics(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    track_id = request.query_params.get("track_id", "")
    # Текст песни выдаёт существование приватного трека и его содержимое.
    filepath = await worker_pool.run_async(_media_filepath, "music_tracks", track_id)
    if filepath and not _visible_to_profile(filepath, _profile_id_of(request)):
        return JSONResponse({"error": "Трек недоступен"}, status_code=403)
    # online=1 — явный запрос на поиск в интернете (долго: перебор источников).
    # Без него отдаём то, что уже сохранено, сразу.
    online = (request.query_params.get("online") or "") in ("1", "true", "yes")
    return await worker_pool.run_async(server.get_lyrics, track_id, online)

@router.get("/api/media/artist-info")
async def api_media_artist_info(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    artist = request.query_params.get("artist", "")
    return await worker_pool.run_async(server.get_artist_info, artist)

@router.get("/api/media/movie-metadata")
async def api_media_movie_metadata(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    title = request.query_params.get("title", "")
    metadata = await worker_pool.run_async(server.get_movie_metadata_cached, title)
    return metadata or {"error": "Не найдено"}

@router.get("/api/media/poster/{poster_id:path}")
async def api_media_poster(poster_id: str):
    import compression_module as _cz
    if not _safe_media_component(poster_id):
        return JSONResponse({"error": "Poster not found"}, status_code=404)
    for ext in ['.jpg', '.jpeg', '.png', '_poster.jpg', '_frame.jpg']:
        poster_path = os.path.join(server.POSTERS_DIR, f"{poster_id}{ext}")
        if _cz.exists(poster_path):
            data = await _aio_read(poster_path, mode='rb')
            ct = 'image/png' if ext == '.png' else 'image/jpeg'
            # Response, не StreamingResponse(io.BytesIO(...)): итерация BytesIO
            # идёт по \n, и 64-КБ JPEG уходил ~250 ASGI-сообщениями вместо одного.
            return Response(content=data, media_type=ct,
                            headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({"error": "Poster not found"}, status_code=404)

@router.get("/api/media/backdrop/{backdrop_id:path}")
async def api_media_backdrop(backdrop_id: str):
    import compression_module as _cz
    if not _safe_media_component(backdrop_id):
        return JSONResponse({"error": "Backdrop not found"}, status_code=404)
    backdrop_path = os.path.join(server.BACKDROPS_DIR, f"{backdrop_id}_backdrop.jpg")
    if _cz.exists(backdrop_path):
        data = await _aio_read(backdrop_path, mode='rb')
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({"error": "Backdrop not found"}, status_code=404)

@router.get("/api/media/cover/{track_id}")
async def api_media_cover(track_id: str, request: Request):
    import compression_module as _cz
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    if not _safe_media_component(track_id):
        return JSONResponse({"error": "Cover not found"}, status_code=404)
    # Обложка — часть приватного трека: чужому профилю не отдаём даже файл.
    filepath = await worker_pool.run_async(_media_filepath, "music_tracks", track_id)
    if filepath and not _visible_to_profile(filepath, _profile_id_of(request)):
        return JSONResponse({"error": "Cover not found"}, status_code=404)
    cover_path = None
    for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        cand = os.path.join(server.COVERS_DIR, f"{track_id}{ext}")
        if _cz.exists(cand):
            cover_path = cand
            break
    if cover_path is None:
        covers = [c for c in glob.glob(os.path.join(server.COVERS_DIR, f"{track_id}.*"))
                  if not c.lower().endswith(_cz.ZST_SUFFIX)]
        if covers:
            cover_path = covers[0]
    # private + Vary: Cookie: браузерный кеш одного профиля не должен отдавать
    # эту обложку другому после переключения профиля в том же окне.
    cover_headers = {"Cache-Control": "private, max-age=86400", "Vary": "Cookie"}
    if cover_path and _cz.exists(cover_path):
        ext = os.path.splitext(cover_path)[1].lower()
        ct = 'image/png' if ext == '.png' else 'image/jpeg'
        data = await _aio_read(cover_path, mode='rb')
        return Response(content=data, media_type=ct, headers=cover_headers)
    cover_path = await worker_pool.run_async(_resolve_track_cover_path, track_id)
    if cover_path:
        ext = os.path.splitext(cover_path)[1].lower()
        ct = 'image/png' if ext == '.png' else 'image/jpeg'
        data = await _aio_read(cover_path, mode='rb')
        return Response(content=data, media_type=ct, headers=cover_headers)
    return JSONResponse({"error": "Cover not found"}, status_code=404)

def _resolve_track_cover_path(track_id: str):
    """Устаревший путь обложки из БД (если файл не скопирован в COVERS_DIR)"""
    try:
        import media_module
        return media_module.resolve_track_cover_path(track_id)
    except Exception:
        swallow.note('legacy_media_player._resolve_track_cover_path:L182')
        return None


# Обложки плейлистов читаются и пишутся прямо из media_module: имена не прошли
# через фасад server (он переживает перенос монолита), а маршрут без них не
# обслуживает ни загрузку, ни авто-выбор.
def _playlist_cover_resolve(playlist_id: str, profile_id=None):
    import media_module
    return media_module.resolve_playlist_cover(playlist_id, profile_id=profile_id)


def _playlist_cover_set(playlist_id: str, image_bytes, profile_id=None):
    import media_module
    return media_module.set_playlist_cover(playlist_id, image_bytes, profile_id=profile_id)


def _playlist_cover_clear(playlist_id: str, profile_id=None):
    import media_module
    return media_module.clear_playlist_cover(playlist_id, profile_id=profile_id)

@router.get("/api/media/duplicates")
async def api_media_duplicates(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    index = await _get_cached_music_index(_profile_id_of(request))
    dupes = await worker_pool.run_async(server.find_duplicates, index.get('tracks', []))
    # UI ждёт массив групп: [ [трек, трек, ...], ... ], а find_duplicates
    # возвращает плоские пары {original, duplicate} — собираем группы здесь.
    groups = {}
    for pair in dupes or []:
        original = pair.get('original') or {}
        duplicate = pair.get('duplicate') or {}
        key = (str(original.get('title', '')).strip().lower(),
               str(original.get('artist', '')).strip().lower())
        group = groups.setdefault(key, [])
        for track in (original, duplicate):
            tid = track.get('id')
            if tid and all(item.get('id') != tid for item in group):
                group.append(track)
    result = list(groups.values())
    return {
        "ok": True,
        "duplicate_groups": result,
        "count": len(result),
        "duplicates": dupes,
        "total_groups": len(result),
    }

@router.get("/api/media/recommendations")
async def api_media_recommendations(request: Request, kind: str = "music",
                                   limit: int = 20, offset: int = 0):
    """Подбор «что слушать/смотреть дальше» для профиля запроса.

    kind=video и kind=series — новая возможность под флагом модуля: выключенный
    модуль не должен отвечать ни в UI, ни по API (тот же порядок, что у чистки
    битого). Музыка считалась и раньше, поэтому её флаг не трогает.
    """
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    pid = _profile_id_of(request)
    kind = (kind or "music").strip().lower()
    if kind not in ("music", "video", "series"):
        kind = "music"
    if kind != "music":
        import module_flags
        if not module_flags.enabled_for(RECOMMEND_MODULE, _flag_pid(request)):
            return JSONResponse({"error": "Рекомендации видео и сериалов выключены",
                                 "available": False, "kind": kind}, status_code=403)
    limit = max(1, min(50, int(limit)))
    offset = max(0, int(offset))
    result = await worker_pool.run_async(server.get_recommendations,
                                         profile_id=pid, kind=kind,
                                         limit=limit, offset=offset)
    if isinstance(result, dict):
        result = dict(result)
        result["kind"] = kind
    return result


@router.get("/api/media/recommend/state")
async def api_media_recommend_state(request: Request):
    """Состояние рубильника для страницы: музыка считается всегда, видео — по флагу."""
    import module_flags
    pid = _flag_pid(request)
    try:
        on = bool(module_flags.enabled_for(RECOMMEND_MODULE, pid))
    except Exception as e:
        logger.warning("Рекомендации: флаг профиля %s не прочитан: %s", pid, e)
        on = False
    return {"ok": True, "music": True, "video": on, "profile": pid}


@router.post("/api/media/recommend/flag")
async def api_media_recommend_flag(request: Request):
    """Переключатель в настройках медиа = флаг модуля этого профиля."""
    import module_flags
    pid = _flag_pid(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or "enabled" not in body:
        return JSONResponse({"error": "Нужно поле enabled"}, status_code=400)
    try:
        module_flags.set_flag_for(RECOMMEND_MODULE, "enabled",
                                  bool(body.get("enabled")), pid)
    except Exception as e:
        logger.warning("Рекомендации: флаг профиля %s не записан: %s", pid, e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "enabled": bool(
        module_flags.enabled_for(RECOMMEND_MODULE, pid)), "profile": pid}

# ---------- Команды плееру извне (агент, скрипты) ----------
# Очередь в памяти: страницы выбирают команды опросом (см.
# static/js/media_playback.js) и исполняют по протоколу media.playback /
# media.command. Для play трек разрешается на сервере — там есть индекс.
_PLAYER_CMD_LIMIT = 50
_player_cmds = {"seq": 0, "queue": []}

@router.post("/api/media/player-command")
async def api_media_player_command(request: Request):
    body = await request.json()
    action = str(body.get("action") or "").strip().lower()
    if action not in ("play", "pause", "resume", "toggle", "next", "prev", "stop", "seek", "volume"):
        return JSONResponse({"error": "Неизвестное действие"})
    payload = None
    if action == "play":
        if not server.MEDIA_AVAILABLE:
            return JSONResponse({"error": "Медиа модуль недоступен"})
        index = await _get_cached_music_index(_profile_id_of(request))
        tracks = index.get("tracks", [])
        track = None
        tid = str(body.get("id") or "")
        if tid:
            track = next((t for t in tracks if str(t.get("id")) == tid), None)
        if track is None:
            query = str(body.get("q") or "").strip()
            if query:
                found = await worker_pool.run_async(server.search_tracks, query, tracks)
                if found:
                    track = found[0]
        if track is None:
            return JSONResponse({"error": "Трек не найден"})
        payload = {"track": {k: track.get(k) for k in ("id", "filepath", "title", "artist", "album", "duration")}}
    elif body.get("value") is not None or body.get("at") is not None:
        payload = {k: body.get(k) for k in ("value", "at") if body.get(k) is not None}
    _player_cmds["seq"] += 1
    cmd = {"id": _player_cmds["seq"], "action": action, "payload": payload,
           "profile": _profile_id_of(request), "ts": time.time()}
    _player_cmds["queue"].append(cmd)
    del _player_cmds["queue"][:-_PLAYER_CMD_LIMIT]
    return {"ok": True, "id": cmd["id"], "track": (payload or {}).get("track")}

@router.get("/api/media/player-commands")
def api_media_player_commands(request: Request):
    try:
        since = int(request.query_params.get("since", "0"))
    except ValueError:
        swallow.note('legacy_media_player.api_media_player_commands:L264')
        since = 0
    # Очередь общая на процесс, а команды адресованы конкретному профилю:
    # без фильтра страница test2 исполняла play, отправленный для test1.
    profile = _profile_id_of(request)
    mine = [c for c in _player_cmds["queue"]
            if c["id"] > since and c.get("profile") == profile]
    latest = max([c["id"] for c in _player_cmds["queue"]
                  if c.get("profile") == profile] or [0])
    return {"latest": latest, "commands": mine}

@router.get("/api/media/favorites")
async def api_media_favorites(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    return await worker_pool.run_async(server.get_favorites,
                                       profile_id=_profile_id_of(request))

@router.get("/api/media/settings")
def api_media_settings_get():
    try:
        from media_module import get_db
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM settings")
            rows = cursor.fetchall()
        finally:
            conn.close()
        return {row["key"]: row["value"] for row in rows}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/media/settings/save")
async def api_media_settings_save(request: Request):
    try:
        from media_module import get_db
        data = await request.json()
        conn = get_db()
        try:
            cursor = conn.cursor()
            for key, value in data.items():
                cursor.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (key, str(value) if not isinstance(value, str) else value)
                )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ---------- Шифрование медиа: API ----------
@router.get("/api/media/encryption/status")
async def api_media_encryption_status():
    import media_crypto
    enabled = _encryption_enabled()
    return {"ok": True, "available": media_crypto.available(),
            "enabled": enabled, "suffix": media_crypto.SUFFIX}


@router.post("/api/media/encryption/settings")
async def api_media_encryption_settings(request: Request):
    import media_crypto
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_media_player.api_media_encryption_settings:L325')
        body = {}
    enabled = 1 if str(body.get("enabled")).strip().lower() in ("1", "true", "yes", "on") else 0
    if enabled and not media_crypto.available():
        return JSONResponse({"ok": False, "error": "Модуль cryptography недоступен"}, status_code=400)
    from media_module import get_db
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            ("media_encryption_enabled", str(enabled)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "enabled": bool(enabled)}


@router.get("/api/media/encryption/list")
async def api_media_encryption_list(request: Request):
    """Треки/видео с признаком «на диске контейнер .infenc»."""
    q = request.query_params
    kind = (q.get("type") or "music").lower()
    search = (q.get("q") or "").strip()
    try:
        limit = max(1, min(int(q.get("limit", 100)), 500))
        offset = max(0, int(q.get("offset", 0)))
    except ValueError:
        swallow.note('legacy_media_player.api_media_encryption_list:L353')
        limit, offset = 100, 0
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    from media_module import get_db

    where, args = "", []
    if search:
        where = " WHERE (title LIKE ? OR artist LIKE ?)"
        like = f"%{search}%"
        args = [like, like]
    conn = get_db()
    try:
        if kind == "video":
            sql = ("SELECT id, filepath, title, filename AS artist FROM video_files"
                   + where.replace("artist", "filename") +
                   " ORDER BY title LIMIT ? OFFSET ?")
            total_sql = "SELECT COUNT(*) FROM video_files" + where.replace("artist", "filename")
        else:
            sql = ("SELECT id, filepath, title, artist FROM music_tracks"
                   + where + " ORDER BY artist, title LIMIT ? OFFSET ?")
            total_sql = "SELECT COUNT(*) FROM music_tracks" + where
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
        total = conn.execute(total_sql, args).fetchone()[0]
    finally:
        conn.close()

    def _flag(path):
        try:
            plain = bool(path) and os.path.isfile(path)
            enc = _has_infenc(path)
            return plain, enc
        except OSError:
            swallow.note('legacy_media_player._flag:L386')
            return False, False

    items = []
    for row in rows:
        path = row["filepath"] or ""
        plain, enc = _flag(path)
        items.append({
            "id": row["id"], "path": path,
            "title": row["title"] or os.path.basename(path or ""),
            "artist": row["artist"] or "",
            "encrypted": bool(enc and not plain),
            "plain": plain,
        })
    return {"ok": True, "type": kind, "total": total, "items": items,
            "enabled": _encryption_enabled()}


@router.post("/api/media/encryption/encrypt")
async def api_media_encryption_encrypt(request: Request):
    import media_crypto
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_media_player.api_media_encryption_encrypt:L410')
        body = {}
    path = str(body.get("path") or "")
    if not _encryption_enabled():
        return JSONResponse({"ok": False, "error": "Шифрование выключено в настройках"}, status_code=403)
    if not _is_known_media_file(path):
        return JSONResponse({"ok": False, "error": "Файл вне медиатеки"}, status_code=403)
    if not _visible_to_profile(path, _profile_id_of(request)):
        return JSONResponse({"ok": False, "error": "Файл вне медиатеки"}, status_code=403)
    if _has_infenc(path) and not os.path.isfile(path):
        return {"ok": True, "already": True, "path": path}
    try:
        res = await worker_pool.run_async(media_crypto.encrypt_file, path)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, **res}


@router.post("/api/media/encryption/decrypt")
async def api_media_encryption_decrypt(request: Request):
    import media_crypto
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_media_player.api_media_encryption_decrypt:L434')
        body = {}
    path = str(body.get("path") or "")
    enc_path = path + media_crypto.SUFFIX
    if not os.path.isfile(enc_path):
        if os.path.isfile(path):
            return {"ok": True, "already": True, "path": path}
        return JSONResponse({"ok": False, "error": "Контейнер не найден"}, status_code=404)
    if not _is_known_media_file(path):
        return JSONResponse({"ok": False, "error": "Файл вне медиатеки"}, status_code=403)
    if not _visible_to_profile(path, _profile_id_of(request)):
        return JSONResponse({"ok": False, "error": "Файл вне медиатеки"}, status_code=403)
    try:
        res = await worker_pool.run_async(media_crypto.decrypt_file, enc_path)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, **res}


@router.get("/api/media/vlc-check")
async def api_media_vlc_check():
    import vlc_streamer
    return await worker_pool.run_async(vlc_streamer.vlc_check)

@router.post("/api/media/vlc-stream/{video_id}")
async def api_media_vlc_stream(video_id: str, request: Request):
    import vlc_streamer
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"ok": False, "error": "Медиа модуль недоступен"}, status_code=400)
    vidx = await _get_cached_video_index(_profile_id_of(request))
    video = next((v for v in vidx.get('videos', []) if v['id'] == video_id), None)
    if not video or not video.get('filepath'):
        return JSONResponse({"ok": False, "error": "Видео не найдено"}, status_code=404)
    filepath = video['filepath']
    source, encrypted = _resolve_media_source(filepath)
    if not source:
        return JSONResponse({"ok": False, "error": f"Файл не найден: {os.path.basename(filepath)}"}, status_code=404)
    tmp = None
    try:
        if encrypted:
            # VLC/ffmpeg читают обычный файл: расшифровываем рядом временный
            # plaintext и удаляем его, когда сессия завершится (stop/status).
            _sweep_enc_temps()
            tmp = await worker_pool.run_async(_decrypt_to_temp, filepath)
            source = tmp
        result = await worker_pool.run_async(vlc_streamer.start_stream, source)
    except Exception as e:
        _drop_temp(tmp)
        logger.error(f"VLC stream start error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if tmp:
        _enc_vlc_temps[result["session_id"]] = tmp
    return JSONResponse({"ok": True, "session_id": result["session_id"],
                         "stream_url": result["stream_url"], "port": result.get("port"),
                         "engine": result.get("engine"), "hls_engine": result.get("hls_engine")})

@router.post("/api/media/vlc-stop/{session_id}")
async def api_media_vlc_stop(session_id: str):
    import vlc_streamer
    ok = await worker_pool.run_async(vlc_streamer.stop_stream, session_id)
    _drop_temp(_enc_vlc_temps.pop(session_id, None))
    return JSONResponse({"ok": ok})

@router.get("/api/media/vlc-status/{session_id}")
async def api_media_vlc_status(session_id: str):
    import vlc_streamer
    info = await worker_pool.run_async(vlc_streamer.get_stream_info, session_id)
    if not info:
        _drop_temp(_enc_vlc_temps.pop(session_id, None))
        return JSONResponse({"ok": False, "error": "Сессия не найдена"}, status_code=404)
    if not info.get('alive'):
        _drop_temp(_enc_vlc_temps.pop(session_id, None))
    return JSONResponse({"ok": True, **info})

@router.get("/api/media/vlc-hls/{session_id}/{filename:path}")
async def api_media_vlc_hls(session_id: str, filename: str, request: Request):
    """Раздача HLS-файлов (m3u8/ts-сегментов) активной VLC-сессии.
    Первый m3u8/ts появляются через 1-4 с после старта VLC — ждём файл до 25 с."""
    import vlc_streamer
    info = vlc_streamer.get_stream_info(session_id)
    if not info or not info.get('dir'):
        return JSONResponse({"error": "Session not found"}, status_code=404)
    base = os.path.abspath(info['dir'])
    target = os.path.normpath(os.path.join(base, filename.replace('\\', '/')))
    if not (target == base or target.startswith(base + os.sep)):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    deadline = time.time() + 25
    while True:
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            break
        info = vlc_streamer.get_stream_info(session_id)
        if not info or not info.get('alive'):
            break
        if time.time() > deadline:
            break
        await asyncio.sleep(0.5)
    if not os.path.isfile(target):
        return JSONResponse({"error": "File not found"}, status_code=404)

    if target.endswith('.m3u8'):
        mime_type = 'application/vnd.apple.mpegurl'
    elif target.endswith('.ts'):
        mime_type = 'video/mp2t'
    else:
        mime_type = mimetypes.guess_type(target)[0] or 'application/octet-stream'
    return await _stream_file_range(target, request, mime_type)

@router.get("/api/media/playlists")
async def api_media_playlists(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    return await worker_pool.run_async(server.get_playlists,
                                       profile_id=_profile_id_of(request))

@router.get("/api/media/index-progress")
def api_media_index_progress():
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    return server.INDEX_PROGRESS

@router.get("/api/media/playlist")
async def api_media_playlist(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    pid = (request.query_params.get("id") or "").strip()
    # Без id WHERE превращался в «все плейлисты профиля», и интерфейс
    # открывал первый попавшийся, а не тот, по которому кликнули.
    if not pid:
        return JSONResponse({"error": "Нужен id плейлиста"}, status_code=400)
    pl = await _run_sync(server.get_playlist, pid, profile_id=_profile_id_of(request))
    if pl is None:
        return JSONResponse({"error": "Плейлист не найден"}, status_code=404)
    return pl

@router.get("/api/media/statistics")
async def api_media_statistics(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    return await _run_sync(server.get_media_statistics,
                           profile_id=_profile_id_of(request))

@router.get("/api/media/random")
async def api_media_random(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    q = request.query_params
    genre = q.get("genre")
    min_year = q.get("min_year")
    max_year = q.get("max_year")
    if min_year: min_year = int(min_year)
    if max_year: max_year = int(max_year)
    video = await _run_sync(server.find_random_video, genre, min_year, max_year,
                            profile_id=_profile_id_of(request))
    return video or {"error": "Ничего не найдено"}

@router.get("/api/media/file-info")
async def api_media_file_info(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    video_id = request.query_params.get("id", "")
    vidx = await _get_cached_video_index(_profile_id_of(request))
    video = next((v for v in vidx.get('videos', []) if v['id'] == video_id), None)
    if video and video.get('filepath'):
        health = await _run_sync(server.check_video_file_health, video['filepath'])
        health['filepath'] = video['filepath']
        health['filename'] = video.get('filename', '')
        health['title'] = video.get('title', '')
        return health
    return JSONResponse({"error": "Видео не найдено"}, status_code=404)

@router.get("/api/media/download")
async def api_media_download(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    video_id = request.query_params.get("id", "")
    vidx = await _get_cached_video_index(_profile_id_of(request))
    video = next((v for v in vidx.get('videos', []) if v['id'] == video_id), None)
    if not video or not video.get('filepath'):
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    fp = video['filepath']
    # Раньше здесь стоял os.path.exists(путь из БД): для зашифрованной
    # библиотеки такого файла на диске нет (лежит <путь>.infenc), и «Скачать»
    # у фильма возвращал 404. Разбор пути и шифрования — один на всех.
    return await _media_file_response(fp, request, video.get('filename') or os.path.basename(fp))

@router.get("/api/media/video-stream/{video_id}")
async def api_media_video_stream(video_id: str, request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    vidx = await _get_cached_video_index(_profile_id_of(request))
    video = next((v for v in vidx.get('videos', []) if v['id'] == video_id), None)
    source, encrypted = _resolve_media_source(video['filepath'] if video else '')
    if video and video.get('filepath') and source:
        filepath = video['filepath']
        mime_type = mimetypes.guess_type(filepath)[0] or 'video/mp4'
        ext = os.path.splitext(filepath)[1].lower()

        # VLC-режим не отдаёт сырые файлы в браузер: AVI/MKV/WMV браузер не декодирует.
        # Такие форматы всегда идут через транскодинг или HLS-движок (см. vlc_streamer).
        # ffprobe внутри needs_transcoding — блокирующий вызов (до 30 с): в потоке
        # событий он подвешивал и сам стрим, и параллельное воспроизведение
        # (слышимые «обрывы»), поэтому уводим проверку в пул потоков.
        # Зашифрованный контейнер ffprobe не прочитает, поэтому transcoding для
        # него решаем по расширению: mp4/webm браузер играет напрямую.
        needs_transcode = True if encrypted and ext not in _BROWSER_VIDEO_EXTS \
            else (False if encrypted else await _run_sync(transcoder.needs_transcoding, filepath))
        if needs_transcode:
            logger.info(f"On-the-fly transcoding for {filepath}")

            def transcode_generator():
                tmp = None
                try:
                    src = filepath
                    if encrypted:
                        tmp = _decrypt_to_temp(filepath)
                        src = tmp
                    for chunk in transcoder.stream_transcode_live(src):
                        yield chunk
                except Exception as e:
                    logger.error(f"Transcoding error for {filepath}: {e}")
                    if not encrypted:
                        try:
                            with open(source, 'rb') as f:
                                while True:
                                    chunk = f.read(65536)
                                    if not chunk:
                                        break
                                    yield chunk
                        except Exception:
                            swallow.note('legacy_media_player.transcode_generator:L655')
                            pass
                finally:
                    _drop_temp(tmp)

            stem = Path(filepath).stem
            try:
                stem.encode('ascii')
                disposition = f'inline; filename="{stem}.mp4"'
            except UnicodeEncodeError:
                swallow.note('legacy_media_player.api_media_video_stream:L665')
                disposition = f'inline; filename="video.mp4"; filename*=UTF-8\'\'{quote(stem)}.mp4'

            return StreamingResponse(
                transcode_generator(),
                media_type='video/mp4',
                headers={
                    'Content-Disposition': disposition,
                    'Cache-Control': 'no-cache',
                    'Accept-Ranges': 'none'
                }
            )

        if encrypted:
            return _stream_encrypted(filepath, request, mime_type)
        return await _stream_file_range(source, request, mime_type)
    return JSONResponse({"error": "Видео не найдено"}, status_code=404)

@router.get("/app/jwt/get")
async def api_jwt_get():
    """JWT token endpoint for TTS services.

    async: страницы пингуют его каждые 10 с (connection_watch.js) — синхронный
    обработчик на каждый пинг занимал поток из threadpool и вставал в очередь
    за тяжёлыми sync-запросами (p95 369 мс при константном ответе).
    """
    return JSONResponse({
        "jwt": "",
        "expires": 0
    })

# Кэш листинга для агентов: бэкенд отвечает ~1.1 с, а Hermes опрашивает ручку
# часто; при зависшем бэкенде запрос стоял ровно сетевой таймаут (10.4 с дважды
# подряд в свепе 200 маршрутов). Схема как у /api/models: 5 с свежего, дальше
# stale-while-revalidate до 10 минут с фоновым обновлением — запрос не ждёт сеть
# никогда, а смена модели видна на следующем опросе.
# dict/set-операции атомарны, отдельного лока не нужно.
_oai_models_cache = {}
_oai_refreshing = set()
_OAI_MODELS_TTL = 5.0
_OAI_MODELS_STALE = 600.0


def _oai_fetch_blocking(base_url, created):
    """Тот же запрос к бэкенду, что и асинхронный путь, но для фонового потока."""
    import urllib.request
    req = urllib.request.Request(
        base_url + "/v1/models",
        headers={"Authorization": "Bearer " + (server.get_llm_api_key() or "")})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return [{"id": item.get("id", ""), "object": "model",
             "created": item.get("created", created),
             "owned_by": item.get("owned_by", "llamacpp")}
            for item in (data.get("data") or [])]


def _oai_refresh_in_background(base_url, created):
    if base_url in _oai_refreshing:
        return
    _oai_refreshing.add(base_url)
    import threading

    def _run():
        try:
            _oai_models_cache[base_url] = {"ts": time.time(),
                                           "data": _oai_fetch_blocking(base_url, created)}
        except Exception as e:
            logger.warning(f"/openai/v1/models: фоновое обновление не удалось ({e})")
        finally:
            _oai_refreshing.discard(base_url)

    threading.Thread(target=_run, name="oai-models-refresh", daemon=True).start()


@router.get("/openai/v1/models")
async def openai_models(request: Request):
    """OpenAI-совместимый листинг моделей для встроенных агентов (Hermes и др.).

    Раньше маршрут отсутствовал, и клиенты, делающие discovery, получали 404
    от статического файлового сервера. Отдаём реальные модели бэкенда,
    сохраняя виртуальное имя ``local-model`` из конфига активного профиля.
    """
    base_url = (await _run_sync(server.get_llm_base_url)).rstrip("/")
    now = int(time.time())
    models = None
    ent = _oai_models_cache.get(base_url)
    if ent is not None:
        age = time.time() - ent["ts"]
        if age <= _OAI_MODELS_TTL:
            models = ent["data"]
        elif age <= _OAI_MODELS_TTL + _OAI_MODELS_STALE:
            models = ent["data"]
            _oai_refresh_in_background(base_url, now)

    if models is None and base_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    base_url + "/v1/models",
                    headers={"Authorization": f"Bearer {await _run_sync(server.get_llm_api_key)}"},
                )
                if resp.status_code == 200:
                    models = []
                    for item in (resp.json() or {}).get("data") or []:
                        models.append({
                            "id": item.get("id", ""),
                            "object": "model",
                            "created": item.get("created", now),
                            "owned_by": item.get("owned_by", "llamacpp"),
                        })
                    _oai_models_cache[base_url] = {"ts": time.time(), "data": models}
        except Exception as e:
            logger.warning(f"/openai/v1/models: бэкенд недоступен ({e})")

    if models is None:
        # бэкенд молчит и кэша нет — пустой список, как было раньше
        models = []

    # Виртуальное имя, на которое настроены агенты, должно быть видно всегда.
    models = list(models)
    ids = {m["id"] for m in models}
    for alias in ("local-model",):
        if alias not in ids:
            models.append({"id": alias, "object": "model", "created": now, "owned_by": "infinity"})

    return JSONResponse({"object": "list", "data": models})


# Reasoning-модели (Qwen3 и подобные) тратят значительную часть бюджета на
# размышление. Ниже этого значения content часто приходит пустым.
_MIN_REASONING_MAX_TOKENS = 1024


@router.post("/openai/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """Прокси на выбранный бэкенд (llamacpp/lmstudio) с живым трекингом генерации"""
    body = await request.body()
    base_url = (await _run_sync(server.get_llm_base_url)).rstrip("/")
    target_url = base_url + "/v1/chat/completions"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers.pop("transfer-encoding", None)
    api_key = await _run_sync(server.get_llm_api_key)
    if api_key:
        # Ключевые заголовки в starlette приходят в нижнем регистре, а запись
        # "Authorization" добавляет SECOND-й заголовок: наружу уходили и ключ
        # клиента, и ключ бэкенда. clodex отвечал на это
        # 400 edge_bad_request "Malformed request" (замер 2026-10-27), локальные
        # бэкенды заголовок просто игнорили — поэтому агент работал только локально.
        for _auth_key in [k for k in headers if k.lower() == "authorization"]:
            headers.pop(_auth_key, None)
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req_body = json.loads(body)
        is_stream = req_body.get("stream", False)
        model_name = str(req_body.get("model", ""))
    except Exception:
        swallow.note('legacy_media_player.openai_chat_completions:L826')
        req_body = None
        is_stream = False
        model_name = ""

    # Живые рассуждения на странице агента (флаг agent_reasoning, по умолчанию ВЫКЛ).
    # Замер /props локального движка: reasoning_format=none — мысли остаются в
    # content, поэтому ни assistant.reasoning, ни снапшот reasoning.available во
    # фронт не доходят. Просим разбор в reasoning_content ТОЛЬКО у запросов,
    # которые сами объявили reasoning_effort: это агент, а не bookgen/голос —
    # те читают content и без флага получают ровно тот же ответ, что раньше.
    if req_body is not None and "reasoning_format" not in req_body:
        try:
            import module_flags
            _local = any(h in base_url for h in ("127.0.0.1", "localhost", "::1"))
            _asked = req_body.get("reasoning_effort") or req_body.get("reasoning")
            if _local and _asked and module_flags.enabled("agent_reasoning"):
                req_body["reasoning_format"] = "deepseek"
                body = json.dumps(req_body).encode("utf-8")
                headers["Content-Length"] = str(len(body))
        except Exception as e:
            logger.warning(f"openai-proxy: reasoning_format не подставлен ({e})")

    # Клиент (Hermes, страница «Агент») всегда зовёт виртуальное имя
    # "local-model". Для облачного бэкенда его нужно менять на модель из
    # настройки «Облачный провайдер»: clodex отвечает 404 model_not_found на
    # любое локальное имя (замер 2026-10-27).
    if req_body is not None:
        try:
            _wanted_model = server.resolve_llm_model(model_name)
            if _wanted_model and _wanted_model != model_name:
                req_body["model"] = _wanted_model
                model_name = _wanted_model
                body = json.dumps(req_body).encode("utf-8")
                headers["Content-Length"] = str(len(body))
        except Exception as e:
            logger.warning(f"openai-proxy: модель не резолвится ({e})")
    module = request.headers.get("x-module") or "openai-proxy"

    # Reasoning-модели тратят токены на размышление до появления ответа.
    # Если клиент прислал крошечный max_tokens (типично для агентов),
    # генерация обрывается на «length» с пустым content. Поднимаем до минимума.
    try:
        _mt = req_body.get("max_tokens")
        if isinstance(_mt, (int, float)) and 0 < int(_mt) < _MIN_REASONING_MAX_TOKENS:
            req_body["max_tokens"] = _MIN_REASONING_MAX_TOKENS
            body = json.dumps(req_body).encode("utf-8")
            headers["Content-Length"] = str(len(body))
    except Exception:
        swallow.note('legacy_media_player.openai_chat_completions:L857')
        pass

    tracker = None
    try:
        from aiengine.api import engine_begin, engine_update_text, engine_end
        prompt = ""
        for m in reversed((req_body.get("messages") or [])):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    prompt = c
                elif isinstance(c, list):
                    prompt = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
                if prompt:
                    break
        engine_begin(module, model_name, prompt=prompt, params={
            "temperature": req_body.get("temperature"),
            "max_tokens": req_body.get("max_tokens"),
            "stream": is_stream,
        })
        tracker = (engine_update_text, engine_end)
    except Exception:
        swallow.note('legacy_media_player.openai_chat_completions:L880')
        pass

    stream_end_kwargs = {}

    def feed_stream_text(chunk_text):
        if not tracker:
            return
        upd = tracker[0]
        for line in chunk_text.splitlines():
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                obj = json.loads(line[6:])
                if "timings" in obj:
                    t = obj.get("timings") or {}
                    if t:
                        stream_end_kwargs.update(tps=t.get("predicted_per_second"),
                                                 tokens=t.get("predicted_n"),
                                                 tokens_prompt=t.get("prompt_n"))
                delta = (obj.get("choices") or [{}])[0].get("delta", {})
                content = delta.get("content") or delta.get("reasoning_content")
                if content:
                    upd(content, token=True)
            except Exception:
                swallow.note('legacy_media_player.feed_stream_text:L906')
                continue

    # Клиент НЕЛЬЗЯ держать в `async with` для стрим-ветки: при return из
    # внутри блока он закрывается ДО того, как генератор начнёт читать поток,
    # и ответ приходит пустым. Поэтому клиент живёт до конца генератора.
    client = httpx.AsyncClient(timeout=300)
    try:
        if is_stream:
            # Поток открываем ДО отдачи StreamingResponse. Раньше статус бэкенда
            # был неизвестен к моменту отправки 200, и клиент получал HTTP 200 с
            # пустым потоком: ни он, ни журнал не видели настоящей причины
            # (замер 2026-10-27: ход Hermes падал 3/3 с «empty stream»).
            upstream = None
            open_error = None

            def dump_rejected_request(payload):
                # Отвергнутый бэкендом запрос сохраняем целиком: без него причину
                # «Malformed request» найти нельзя, а повторный ход агента стоит
                # перезапуска и денег на запросах.
                try:
                    _rej_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "logs")
                    os.makedirs(_rej_dir, exist_ok=True)
                    with open(os.path.join(_rej_dir, "openai_stream_reject.json"), "wb") as _rf:
                        _rf.write(payload[:400000])
                except Exception as _re:
                    logger.warning(f"openai-proxy: не удалось сохранить отвергнутый запрос ({_re})")

            try:
                upstream = await client.send(
                    client.build_request("POST", target_url, content=body, headers=headers),
                    stream=True,
                )
            except Exception as e:
                swallow.note('legacy_media_player.openai_chat_completions:L940', e)
                open_error = e

            async def release_stream():
                if upstream is not None:
                    try:
                        await upstream.aclose()
                    except Exception:
                        swallow.note('legacy_media_player.release_stream:L948')
                        pass
                try:
                    await client.aclose()
                except Exception:
                    swallow.note('legacy_media_player.release_stream:L953')
                    pass
                if tracker:
                    try:
                        tracker[1]("", **stream_end_kwargs)
                    except Exception:
                        swallow.note('legacy_media_player.release_stream:L959')
                        pass

            if open_error is not None:
                logger.error(f"OpenAI stream: бэкенд не открыл поток "
                             f"({type(open_error).__name__}: {open_error}) | model={model_name} bytes_in={len(body)} "
                             f"req_head={body[:400].decode('utf-8', 'replace')}")
                dump_rejected_request(body)
                await release_stream()
                return JSONResponse(status_code=502, content={"error": {
                    "message": f"Бэкенд не открыл поток: {type(open_error).__name__}: {open_error}",
                    "type": "proxy_error"}})

            if upstream.status_code != 200:
                try:
                    raw = await upstream.aread()
                except Exception:
                    swallow.note('legacy_media_player.openai_chat_completions:L976')
                    raw = b""
                backend_text = raw.decode("utf-8", "replace")[:500]
                logger.error(f"OpenAI stream: бэкенд ответил HTTP {upstream.status_code}: "
                             f"{backend_text} | model={model_name} bytes_in={len(body)} "
                             f"req_head={body[:400].decode('utf-8', 'replace')}")
                dump_rejected_request(body)
                await release_stream()
                try:
                    payload = json.loads(raw)
                except Exception:
                    swallow.note('legacy_media_player.openai_chat_completions:L987')
                    payload = {"error": {"message": backend_text or f"HTTP {upstream.status_code}",
                                         "type": "backend_error"}}
                return JSONResponse(status_code=upstream.status_code, content=payload)

            async def event_stream():
                try:
                    async for chunk in upstream.aiter_bytes():
                        text = chunk.decode("utf-8", errors="replace")
                        feed_stream_text(text)
                        yield text
                except Exception as e:
                    # str(e) у обрыва соединения бывает пустым — без имени класса
                    # в журнал и к клиенту уходило «{"error": ""}", то есть
                    # нечитаемый «[object Object]» в интерфейсе.
                    reason = str(e).strip() or f"{type(e).__name__}: бэкенд оборвал поток без finish_reason"
                    logger.error(f"OpenAI stream error ({type(e).__name__}): {e} | model={model_name} "
                                 f"bytes_in={len(body)} params={sorted((req_body or {}).keys())} "
                                 f"req_head={body[:400].decode('utf-8', 'replace')}")
                    yield "data: " + json.dumps({"error": {"message": reason,
                                                           "type": "proxy_stream_error"}}, ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    await release_stream()
            return StreamingResponse(event_stream(), media_type="text/event-stream")
        else:
            resp = await client.post(target_url, content=body, headers=headers)
            data = resp.json()
            await client.aclose()

                # Reasoning-модели (Qwen3 и т.п.) могут вернуть пустой content,
            # потратив весь max_tokens на размышление (finish_reason="length").
            # Клиент (Hermes и др.) тогда показывает пустой ответ — «думает,
            # но ответа нет». Отдаём размышление как текст, чтобы не терять его.
            try:
                for ch in data.get("choices") or []:
                    msg = ch.get("message") or {}
                    if not (msg.get("content") or "").strip() and msg.get("reasoning_content"):
                        msg["content"] = msg["reasoning_content"]
                        if ch.get("finish_reason") == "length":
                            ch["finish_reason"] = "stop"
            except Exception:
                swallow.note('legacy_media_player.openai_chat_completions:L1029')
                pass

            if tracker:
                try:
                    msg = (data.get("choices") or [{}])[0].get("message") or {}
                    content = msg.get("content") or msg.get("reasoning_content") or ""
                    if content:
                        tracker[0](content)
                    timings = data.get("timings") or {}
                    tracker[1]("", tps=timings.get("predicted_per_second"),
                               tokens=timings.get("predicted_n"),
                               tokens_prompt=timings.get("prompt_n"))
                except Exception:
                    swallow.note('legacy_media_player.openai_chat_completions:L1043')
                    pass
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as e:
        logger.error(f"OpenAI proxy error: {e}")
        if tracker:
            try:
                tracker[1]("", error=str(e))
            except Exception:
                swallow.note('legacy_media_player.openai_chat_completions:L1052')
                pass
        return JSONResponse({"error": str(e)}, status_code=502)
    finally:
        if tracker and not is_stream:
            try:
                tracker[1]()
            except Exception:
                swallow.note('legacy_media_player.openai_chat_completions:L1060')
                pass

@router.post("/openai/v1/moderations")
def openai_moderations():
    """Заглушка модерации — всегда пропускать"""
    return JSONResponse({
        "results": [{
            "flagged": False,
            "categories": {
                "sexual": False, "hate": False, "violence": False,
                "self-harm": False, "sexual/minors": False,
                "hate/threatening": False, "violence/graphic": False
            },
            "category_scores": {
                "sexual": 0.0, "hate": 0.0, "violence": 0.0,
                "self-harm": 0.0, "sexual/minors": 0.0,
                "hate/threatening": 0.0, "violence/graphic": 0.0
            }
        }]
    })

_MEDIA_STREAM_EXTS = {
    '.mp3', '.flac', '.ogg', '.oga', '.wav', '.aac', '.m4a', '.wma', '.opus',
    '.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.wmv', '.flv',
    '.ts', '.m2ts', '.ogv',
}


def _safe_media_component(value: str) -> bool:
    """id/имя файла без разделителей пути (защита от ..\\ и абсолютных путей)."""
    return bool(value) and os.path.basename(value) == value and value not in (".", "..")


def _is_under_media_roots(path: str) -> bool:
    try:
        real = os.path.normcase(os.path.realpath(path))
    except Exception:
        swallow.note('legacy_media_player._is_under_media_roots:L1098')
        return False
    roots = []
    for attr in ("MUSIC_DIRS", "VIDEO_DIRS"):
        try:
            roots.extend(getattr(server, attr) or [])
        except Exception:
            swallow.note('legacy_media_player._is_under_media_roots:L1105')
            pass
    for root in roots:
        try:
            rr = os.path.normcase(os.path.realpath(root))
        except Exception:
            swallow.note('legacy_media_player._is_under_media_roots:L1111')
            continue
        if rr and (real == rr or real.startswith(rr + os.sep)):
            return True
    return False


def _media_path_variants(path: str):
    """Варианты записи одного пути для поиска в БД.

    Индексатор сохраняет пути с прямыми слешами (resolve_media_path →
    ensure_local_path), а realpath на Windows отдаёт обратные. Раньше запрос
    сравнивал только realpath, поэтому свежепроиндексированный трек не
    находился и стрим отвечал 403 до перезапуска сервера (пока media-корни
    не перечитывались из конфига).
    """
    out = []
    try:
        out.append(os.path.realpath(path))
        out.append(os.path.abspath(path))
        out.append(os.path.normpath(path))
    except Exception:
        swallow.note('legacy_media_player._media_path_variants:L1133')
        pass
    out.append(path)
    variants = []
    for p in out:
        if not p:
            continue
        for v in (p, p.replace('\\', '/')):
            if v not in variants:
                variants.append(v)
    return variants


def _visible_to_profile(path: str, profile_id) -> bool:
    """Пускаем ли этот профиль к файлу (см. media_visibility.stream_allowed).

    Прямой путь в /api/media/stream раньше проверял только «файл известен
    медиатеке» — гость без куки мог стримить чужой приватный трек, просто зная
    путь. Скрытый от профиля файл теперь и не отдаётся, и не скачивается.
    """
    try:
        import media_visibility as MV
        return MV.stream_allowed(path, profile_id)
    except Exception:
        logger.exception("media_visibility: видимость пути не проверена, пускаем как раньше")
        return True


def _media_filepath(table: str, item_id: str):
    """Путь файла из music_tracks/video_files по id; None — записи нет."""
    try:
        import media_module
        conn = media_module.get_db()
        try:
            row = conn.execute(
                f"SELECT filepath FROM {table} WHERE id = ?", (str(item_id),)
            ).fetchone()
            return row["filepath"] if row else None
        finally:
            conn.close()
    except Exception:
        swallow.note('legacy_media_player._media_filepath:L1187')
        return None


def _is_known_media_file(path: str) -> bool:
    """Файл проиндексирован или лежит в настроенных медиа-корнях.

    Зашифрованный вариант (<path>.infenc) считается тем же файлом: в базе
    путь хранится исходным, а на диске остаётся только контейнер.
    """
    if not path or os.path.splitext(path)[1].lower() not in _MEDIA_STREAM_EXTS:
        return False
    try:
        if not os.path.isfile(path):
            if not _has_infenc(path):
                return False
    except OSError:
        swallow.note('legacy_media_player._is_known_media_file:L1174')
        return False
    try:
        import media_module
        variants = _media_path_variants(path)
        marks = ','.join('?' for _ in variants)
        conn = media_module.get_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM music_tracks WHERE filepath IN ({m}) "
                "UNION ALL SELECT 1 FROM video_files WHERE filepath IN ({m}) LIMIT 1".format(m=marks),
                variants + variants,
            ).fetchone()
        finally:
            conn.close()
        if row:
            return True
    except Exception:
        swallow.note('legacy_media_player._is_known_media_file:L1192')
        pass
    return _is_under_media_roots(path)


def _has_infenc(path: str) -> bool:
    """Есть ли рядом зашифрованный контейнер для этого пути."""
    try:
        return bool(path) and os.path.isfile(path + '.infenc')
    except OSError:
        swallow.note('legacy_media_player._has_infenc:L1202')
        return False


# ---------- Опциональное шифрование медиа ----------
# Файл на диске лежит контейнером <имя>.infenc (см. media_crypto), а в БД
# остаётся исходный путь. Плеер ходит по тому же /api/media/stream и получает
# расшифрованные байты; Range поддерживается без расшифровки файла целиком.

_MEDIA_CONTENT_TYPES = {
    '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg', '.wav': 'audio/wav', '.aac': 'audio/aac',
    '.m4a': 'audio/mp4', '.wma': 'audio/x-ms-wma', '.opus': 'audio/opus',
    '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mkv': 'video/x-matroska',
    '.webm': 'video/webm', '.avi': 'video/x-msvideo', '.mov': 'video/quicktime',
    '.wmv': 'video/x-ms-wmv', '.flv': 'video/x-flv',
    '.ts': 'video/mp2t', '.m2ts': 'video/mp2t', '.ogv': 'video/ogg',
}

_BROWSER_VIDEO_EXTS = {'.mp4', '.m4v', '.webm'}

# Временные расшифровки для ffmpeg/VLC: plaintext живёт только на время сессии.
_enc_vlc_temps = {}


def _media_content_type(path: str) -> str:
    return _MEDIA_CONTENT_TYPES.get(os.path.splitext(path)[1].lower(),
                                    'application/octet-stream')


def _resolve_media_source(path: str):
    """Реальные байты файла: (путь, зашифрован_ли) или (None, False)."""
    try:
        if os.path.isfile(path):
            return path, False
    except OSError:
        swallow.note('legacy_media_player._resolve_media_source:L1238')
        pass
    if _has_infenc(path):
        return path + '.infenc', True
    return None, False


def _encryption_enabled() -> bool:
    try:
        import media_module
        val = media_module.get_setting_value('media_encryption_enabled', '0')
        return str(val).strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        swallow.note('legacy_media_player._encryption_enabled:L1251')
        return False


def _parse_range(range_header: str, file_size: int):
    """(start, end) из заголовка Range; None — заголовок не разобран."""
    m = re.match(r'bytes=(\d*)-(\d*)', (range_header or '').strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    if m.group(1) == '':
        n = int(m.group(2))
        return max(0, file_size - n), file_size - 1
    start = int(m.group(1))
    end = min(int(m.group(2)), file_size - 1) if m.group(2) != '' else file_size - 1
    return start, end


def _stream_encrypted(path: str, request: Request, media_type: str):
    """Отдать расшифрованный контейнер с поддержкой Range."""
    import media_crypto
    enc_path = path + media_crypto.SUFFIX
    file_size = media_crypto.plain_size(enc_path)
    range_header = request.headers.get('range')
    start, end = 0, file_size - 1
    status_code = 200
    if range_header:
        parsed = _parse_range(range_header, file_size)
        if parsed:
            start, end = parsed
            status_code = 206
        if start > end or start >= file_size:
            return JSONResponse({"error": "Range Not Satisfiable"}, status_code=416,
                                headers={"Content-Range": f"bytes */{file_size}"})
    remaining = end - start + 1

    async def _iter():
        loop = asyncio.get_event_loop()
        it = media_crypto.iter_decrypted(enc_path, start, remaining)
        while True:
            chunk = await loop.run_in_executor(None, next, it, None)
            if chunk is None:
                break
            yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(remaining),
        "Cache-Control": "no-cache",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(_iter(), status_code=status_code,
                             media_type=media_type, headers=headers)


def _decrypt_to_temp(path: str) -> str:
    """Расшифровать контейнер во временный файл (для ffmpeg/VLC).

    Возвращает путь к plaintext-файлу в TRANSCODE_DIR; вызывающий обязан
    удалить его после завершения чтения.
    """
    import media_crypto
    import tempfile
    from media_module import TRANSCODE_DIR
    enc_path = path + media_crypto.SUFFIX
    os.makedirs(str(TRANSCODE_DIR), exist_ok=True)
    suffix = os.path.splitext(path)[1] or '.bin'
    fd, tmp = tempfile.mkstemp(prefix='dec_', suffix=suffix, dir=str(TRANSCODE_DIR))
    os.close(fd)
    try:
        with open(tmp, 'wb') as fo:
            for chunk in media_crypto.iter_decrypted(enc_path):
                fo.write(chunk)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            swallow.note('legacy_media_player._decrypt_to_temp:L1328')
            pass
        raise
    return tmp


def _drop_temp(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        swallow.note('legacy_media_player._drop_temp:L1339')
        pass


def _sweep_enc_temps():
    """Удалить plaintext-времена от прерванных сессий (старше 6 часов)."""
    try:
        from media_module import TRANSCODE_DIR
        deadline = time.time() - 6 * 3600
        for name in os.listdir(str(TRANSCODE_DIR)):
            if not name.startswith('dec_'):
                continue
            full = os.path.join(str(TRANSCODE_DIR), name)
            try:
                if os.path.isfile(full) and os.path.getmtime(full) < deadline:
                    os.remove(full)
            except OSError:
                swallow.note('legacy_media_player._sweep_enc_temps:L1356')
                pass
    except Exception:
        swallow.note('legacy_media_player._sweep_enc_temps:L1359')
        pass


def _is_allowed_subtitle_path(path: str) -> bool:
    if not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in (".srt", ".vtt"):
        if _is_under_media_roots(path) or _is_known_media_file(path):
            return True
        base = os.path.splitext(path)[0]
        return any(_is_known_media_file(base + e) for e in _MEDIA_STREAM_EXTS)
    # Сюда приходит и путь видео без расширения — для извлечения embedded-субтитров.
    if _is_known_media_file(path):
        return True
    return any(_is_known_media_file(path + e) for e in _MEDIA_STREAM_EXTS)


def _decode_subtitle_path(raw: str) -> str:
    """Декод пути субтитров: фронтенд шлёт base64url(путь без расширения) + '.srt'/'.vtt'.
    Поддерживает и легаси-вызовы с прямым путём."""
    import base64
    candidates = []
    if raw.lower().endswith(('.srt', '.vtt')):
        candidates.append(raw[:-4])
    candidates.append(raw)
    for cand in candidates:
        try:
            b64 = cand.replace('-', '+').replace('_', '/')
            b64 += '=' * (-len(b64) % 4)
            path = base64.b64decode(b64).decode('utf-8')
            if '/' in path:
                return path
        except Exception:
            swallow.note('legacy_media_player._decode_subtitle_path:L1394')
            continue
    try:
        return unquote(raw)
    except Exception:
        swallow.note('legacy_media_player._decode_subtitle_path:L1399')
        return raw

@router.get("/api/media/subtitle/{file_path:path}")
async def api_media_subtitle(file_path: str, request: Request):
    """Serve subtitle files (.srt, .vtt)"""
    import base64
    decoded_path = _decode_subtitle_path(file_path)

    if not _is_allowed_subtitle_path(decoded_path):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not _visible_to_profile(decoded_path, _profile_id_of(request)):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    import compression_module as _cz
    if not _cz.exists(decoded_path):
        vtt_path = await worker_pool.run_async(_extract_embedded_subtitles, decoded_path)
        if vtt_path:
            decoded_path = vtt_path

    if not _cz.exists(decoded_path):
        return JSONResponse({"error": "Subtitle not found"}, status_code=404)

    ext = os.path.splitext(decoded_path)[1].lower()
    media_type = 'text/vtt' if ext == '.vtt' else 'text/plain; charset=utf-8'

    if _cz.is_compressed(decoded_path):
        data = _cz.read_bytes(decoded_path)
    else:
        data = await worker_pool.read_file(decoded_path, mode="rb")

    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"}
    )

def _extract_embedded_subtitles(subtitle_path: str):
    """Если внешнего файла субтитров нет — пробуем извлечь embedded-субтитры
    из видео-файла (mkv/mp4) через ffmpeg в .vtt (кэш ~/.infinity/data/subtitles)."""
    try:
        import media_module
        sub_dir = media_module.SUBTITLES_DIR
        os.makedirs(sub_dir, exist_ok=True)
    except Exception:
        swallow.note('legacy_media_player._extract_embedded_subtitles:L1444')
        sub_dir = os.path.join(os.path.expanduser('~/.infinity'), 'data', 'subtitles')
    try:
        os.makedirs(sub_dir, exist_ok=True)
    except Exception:
        swallow.note('legacy_media_player._extract_embedded_subtitles:L1449')
        sub_dir = os.path.join(os.path.expanduser('~'), '.infinity', 'data', 'subtitles')

    base = os.path.splitext(subtitle_path)[0]
    for ext in ('.mkv', '.mp4', '.m4v', '.avi', '.mov', '.webm', '.ts', '.flv', '.wmv'):
        video = base + ext
        if os.path.exists(video):
            break
    else:
        return None

    hash_val = hashlib.md5(video.encode('utf-8')).hexdigest()[:16]
    cache = os.path.join(sub_dir, f"{hash_val}.vtt")
    if os.path.exists(cache):
        return cache

    if not shutil.which('ffprobe'):
        return None
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 's',
             '-show_entries', 'stream=index:stream_tags=language,title',
             '-of', 'json', video],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        streams = json.loads(r.stdout or '{}').get('streams', [])
    except Exception:
        swallow.note('legacy_media_player._extract_embedded_subtitles:L1477')
        return None
    if not streams:
        return None

    # Выбираем русский трек, иначе первый
    chosen = None
    for s in streams:
        lang = (s.get('tags') or {}).get('language', '') or ''
        if lang.lower().startswith('ru'):
            chosen = s
            break
    if chosen is None:
        chosen = streams[0]
    index = chosen.get('index')

    try:
        r2 = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-i', video, '-map', f'0:{index}', '-f', 'webvtt', cache],
            capture_output=True, text=True, timeout=120)
    except Exception:
        swallow.note('legacy_media_player._extract_embedded_subtitles:L1498')
        return None
    if r2.returncode != 0 or not os.path.exists(cache):
        # Битовые субтитры (PGS/DVD) в webvtt не конвертируются
        return None
    return cache

@router.post("/api/media/transcode/{video_id}")
async def api_media_transcode_start(video_id: str):
    """Запустить фоновое перекодирование видео в MP4."""
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"}, status_code=503)
    from media_module import start_transcode_server, TRANSCODE_DIR, state
    output_path = os.path.join(str(TRANSCODE_DIR), f"{video_id}.mp4")
    if os.path.exists(output_path):
        return JSONResponse({"ok": True, "message": "Уже перекодирован", "cached": True})
    if state.transcoding_active.get(video_id):
        return JSONResponse({"ok": True, "message": "Перекодирование уже идёт", "cached": False})
    result = await worker_pool.run_async(start_transcode_server, video_id)
    if result.get('error'):
        return JSONResponse({"error": result['error']}, status_code=404)
    return JSONResponse(result)


@router.get("/api/media/transcode-progress/{video_id}")
def api_media_transcode_progress(video_id: str):
    """Прогресс перекодирования видео (данные пишет media_module)."""
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"}, status_code=503)
    from media_module import TRANSCODE_DIR, state
    output_path = os.path.join(str(TRANSCODE_DIR), f"{video_id}.mp4")
    active = bool(state.transcoding_active.get(video_id))
    info = state.transcoding_progress.get(video_id) or {}
    if os.path.exists(output_path):
        if not active:
            _cache_invalidate("video_index")
        return JSONResponse({
            "active": active,
            "progress": 100 if not active else info.get('progress', 0),
            "current": info.get('current', 0),
            "duration": info.get('duration', 0),
            "done": not active,
        })
    return JSONResponse({
        "active": active,
        "progress": info.get('progress', 0),
        "current": info.get('current', 0),
        "duration": info.get('duration', 0),
        "done": False,
    })


@router.get("/api/torrent/stream")
async def api_torrent_stream(request: Request):
    """Прогрессивный стрим торрент-файла через helper (не блокирует сервер).

    Читает данные из webtorrent-хелпера чанками и отдаёт клиенту сразу,
    не буферизуя весь файл в RAM — плеер начинает играть после первых кусков.
    """
    from server.torrent import TORRENT_HELPER_PORT, ensure_torrent_helper
    info_hash = request.query_params.get("hash", "")
    file_index = request.query_params.get("file", "")
    if not info_hash or file_index == "":
        return JSONResponse({"error": "Missing hash or file parameter"}, status_code=400)

    ok = await worker_pool.run_async(ensure_torrent_helper)
    if not ok:
        return JSONResponse({"error": "Torrent helper недоступен"}, status_code=503)

    proxy_url = f"http://127.0.0.1:{TORRENT_HELPER_PORT}/stream/{info_hash}/{file_index}"
    upstream_headers = {}
    for key in ("Range", "If-Range", "If-Modified-Since", "If-None-Match"):
        val = request.headers.get(key)
        if val:
            upstream_headers[key] = val

    try:
        client = httpx.AsyncClient(timeout=None)
        req = client.build_request("GET", proxy_url, headers=upstream_headers)
        # stream=True — отдаём клиенту по мере поступления кусочков от helper,
        # а не после полного скачивания файла (иначе плеер ждал бы всё).
        upstream = await client.send(req, stream=True)
    except Exception as e:
        return JSONResponse({"error": f"Stream proxy error: {e}"}, status_code=502)

    resp_headers = {}
    for key in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control"):
        val = upstream.headers.get(key)
        if val:
            resp_headers[key] = val

    async def _torrent_iter():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(_torrent_iter(), status_code=upstream.status_code, headers=resp_headers)

@router.get("/api/torrent/stream-transcoded")
async def api_torrent_stream_transcoded(request: Request):
    """Тот же торрент-файл, но через ffmpeg.

    Chromium нестабильно играет MKV/AVI/WMV из торрента (рывки, обрывы): контейнер
    и кодек он либо не декодирует, либо декодирует с провалами. Отдаём
    транскодированный в fragmented mp4 поток — так же, как для локального видео
    в /api/media/video-stream.
    """
    from server.torrent import TORRENT_HELPER_PORT, ensure_torrent_helper
    import transcoder
    info_hash = request.query_params.get("hash", "")
    file_index = request.query_params.get("file", "")
    if not info_hash or file_index == "":
        return JSONResponse({"error": "Missing hash or file parameter"}, status_code=400)

    ok = await worker_pool.run_async(ensure_torrent_helper)
    if not ok:
        return JSONResponse({"error": "Torrent helper недоступен"}, status_code=503)

    src_url = f"http://127.0.0.1:{TORRENT_HELPER_PORT}/stream/{info_hash}/{file_index}"

    def _gen():
        try:
            for chunk in transcoder.stream_transcode_live(src_url):
                yield chunk
        except Exception as e:
            logger.error(f"Torrent transcode error ({info_hash}/{file_index}): {e}")

    return StreamingResponse(
        _gen(),
        media_type='video/mp4',
        headers={'Cache-Control': 'no-cache', 'Accept-Ranges': 'none'},
    )

@router.get("/api/media/stream/{file_path_encoded:path}")
async def api_media_stream(file_path_encoded: str, request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    try:
        b64 = file_path_encoded.replace('-', '+').replace('_', '/')
        b64 += '=' * (-len(b64) % 4)
        file_path = base64.b64decode(b64).decode('utf-8')
    except Exception:
        swallow.note('legacy_media_player.api_media_stream:L1643')
        try:
            file_path = unquote(file_path_encoded)
        except Exception:
            swallow.note('legacy_media_player.api_media_stream:L1647')
            file_path = file_path_encoded
    if not _is_known_media_file(file_path):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not _visible_to_profile(file_path, _profile_id_of(request)):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    source, encrypted = _resolve_media_source(file_path)
    if source:
        if request.query_params.get("transcode") in ("1", "true", "yes"):
            # Локальный файл вне медиатеки (например, только что скачанный торрентом MKV):
            # браузер такой контейнер не декодирует, поэтому гоним через ffmpeg.
            def _transcode_gen():
                tmp = None
                try:
                    src = file_path
                    if encrypted:
                        # ffmpeg не понимает контейнер: расшифровываем во временный
                        # файл и удаляем его сразу после закрытия потока.
                        tmp = _decrypt_to_temp(file_path)
                        src = tmp
                    for chunk in transcoder.stream_transcode_live(src):
                        yield chunk
                except Exception as e:
                    logger.error(f"Local stream transcode error ({file_path}): {e}")
                finally:
                    _drop_temp(tmp)
            return StreamingResponse(
                _transcode_gen(),
                media_type='video/mp4',
                headers={'Cache-Control': 'no-cache', 'Accept-Ranges': 'none'},
            )
        ct = _media_content_type(file_path)
        if encrypted:
            return _stream_encrypted(file_path, request, ct)
        return await _stream_file_range(source, request, ct)
    return JSONResponse({"error": "Файл не найден"}, status_code=404)

@router.get("/api/media/collections")
async def api_media_collections(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    from media_module import get_collections_direct
    fn = server.get_collections_direct if hasattr(server, 'get_collections_direct') else get_collections_direct
    return await worker_pool.run_async(fn, profile_id=_profile_id_of(request))

@router.get("/api/media/favorites/videos")
async def api_media_favorites_videos(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    from media_module import get_favorite_videos
    return await worker_pool.run_async(get_favorite_videos,
                                       profile_id=_profile_id_of(request))

@router.get("/api/media/series/{series_name:path}")
async def api_media_series(series_name: str):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    series_name = unquote(series_name)
    from media_module import get_series_meta
    return await worker_pool.run_async(get_series_meta, series_name)

@router.get("/api/media/collections/{collection_id}")
async def api_media_collection_get(collection_id: str, request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    from media_module import get_collection_direct
    result = await worker_pool.run_async(get_collection_direct, collection_id,
                                         profile_id=_profile_id_of(request))
    return result or {"error": "Collection not found"}

# Media POST endpoints
def _profile_id_of(request):
    """Профиль вызывающего: cookie → токен агента → общедоступный.

    Активный профиль сервера здесь не используется намеренно: гость из LAN без
    куки должен видеть общедоступную библиотеку, а не приватную библиотеку того,
    кто последним менял профиль. Тот же резолв используют запись каталогов
    (rebuild-ручки) и фильтр выдачи, поэтому «видно» и «открывается» совпадают.
    """
    try:
        import media_visibility as MV
        return MV.profile_of_request(request)
    except Exception:
        logger.exception("media_visibility: профиль из запроса не определён")
        return None


@router.post("/api/media/rebuild-music")
async def api_media_rebuild_music(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    dirs = body.get('dirs', [])
    pid = _profile_id_of(request)
    if not dirs:
        # Раньше здесь брался media.* АКТИВНОГО профиля сервера: вызов без
        # списка (API, гонка настроек) записывал чужие каталоги в профиль
        # вызывающего — и его приватная музыка становилась видна другому.
        import media_visibility as MV
        dirs = MV.visible_dirs('music', pid)

    def _rebuild_music():
        # Каталог сохраняется как СВОЙ ключ профиля (private_music_dirs), а не
        # в media.*: иначе общедоступная строка записалась бы в профиль как
        # собственная, а при снятии наследования пропала бы и база, и настройки.
        # Индексируем уже эффективный список, чтобы общедоступные папки остались
        # в библиотеке.
        from modules.profile_state import persist_media_dirs
        eff = persist_media_dirs("music_dirs", dirs, profile_id=pid)
        if eff is None:
            eff = list(dirs)
        tracks = server.rebuild_music_index(eff)
        _cache_invalidate("music_index")
        return {"ok": True, "tracks": len(tracks)}

    task_id = worker_pool.background_task(_rebuild_music)
    return {"ok": True, "message": "Перестроение музыкального индекса запущено в фоне", "task_id": task_id}

@router.post("/api/media/rebuild-video")
async def api_media_rebuild_video(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    dirs = body.get('dirs', [])
    pid = _profile_id_of(request)
    if not dirs:
        # Тот же дефект, что у rebuild-music: чужой активный каталог не должен
        # записываться в профиль вызывающего.
        import media_visibility as MV
        dirs = MV.visible_dirs('video', pid)

    def _rebuild_video():
        from modules.profile_state import persist_media_dirs
        eff = persist_media_dirs("video_dirs", dirs, profile_id=pid)
        if eff is None:
            eff = list(dirs)
        videos = server.rebuild_video_index(eff)
        _cache_invalidate("video_index")
        return {"ok": True, "videos": len(videos)}

    task_id = worker_pool.background_task(_rebuild_video)
    return {"ok": True, "message": "Перестроение видео индекса запущено в фоне", "task_id": task_id}

@router.post("/api/media/favorites/add")
async def api_media_favorites_add(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await worker_pool.run_async(server.add_to_favorites, body.get('id', ''), body.get('type', 'track'),
                                         profile_id=_profile_id_of(request))
    return {"ok": result}

@router.post("/api/media/favorites/remove")
async def api_media_favorites_remove(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await worker_pool.run_async(server.remove_from_favorites, body.get('id', ''), body.get('type', 'track'),
                                         profile_id=_profile_id_of(request))
    return {"ok": result}

@router.post("/api/media/playlist/create")
async def api_media_playlist_create(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    playlist = await worker_pool.run_async(server.create_playlist, body.get('name', ''), body.get('description', ''), body.get('tracks', []),
                                           profile_id=_profile_id_of(request))
    return {"ok": True, "playlist": playlist}

@router.post("/api/media/playlist/update")
async def api_media_playlist_update(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    playlist = await _run_sync(server.update_playlist, body.get('id', ''), body,
                               profile_id=_profile_id_of(request))
    if playlist is None:
        return JSONResponse({"error": "Плейлист не найден"}, status_code=404)
    return {"ok": True, "playlist": playlist}

@router.post("/api/media/playlist/add-track")
async def api_media_playlist_add_track(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await _run_sync(server.add_track_to_playlist, body.get('playlist_id', ''), body.get('track_id', ''),
                             profile_id=_profile_id_of(request))
    return {"ok": result}

@router.post("/api/media/playlist/remove-track")
async def api_media_playlist_remove_track(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await _run_sync(server.remove_track_from_playlist, body.get('playlist_id', ''), body.get('track_id', ''),
                             profile_id=_profile_id_of(request))
    return {"ok": result}

@router.post("/api/media/playlist/delete")
async def api_media_playlist_delete(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await _run_sync(server.delete_playlist, body.get('id', ''),
                             profile_id=_profile_id_of(request))
    return {"ok": result}


@router.get("/api/media/playlist/cover/{playlist_id}")
async def api_media_playlist_cover(playlist_id: str, request: Request):
    """Обложка плейлиста: своя, иначе трек с максимумом прослушиваний."""
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    if not _safe_media_component(playlist_id):
        return JSONResponse({"error": "Cover not found"}, status_code=404)
    resolved = await _run_sync(_playlist_cover_resolve, playlist_id,
                               profile_id=_profile_id_of(request))
    path, kind, track_id = (resolved or (None, "", ""))
    if kind == "user" and path:
        try:
            data = await _aio_read(path, mode="rb")
        except FileNotFoundError:
            return JSONResponse({"error": "Cover not found"}, status_code=404)
        ext = os.path.splitext(path)[1].lower()
        return Response(content=data,
                        media_type="image/png" if ext == ".png" else "image/jpeg",
                        # Свою обложку пересылаем при каждом открытии карточки:
                        # URL не меняется, а вчерашняя картинка после загрузки
                        # новой осталась бы в кеше браузера.
                        headers={"Cache-Control": "private, max-age=0, must-revalidate",
                                 "Vary": "Cookie"})
    if kind == "auto" and track_id:
        return await api_media_cover(track_id, request)
    return JSONResponse({"error": "Cover not found"}, status_code=404)


@router.post("/api/media/playlist/cover")
async def api_media_playlist_cover_upload(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    playlist_id = body.get('playlist_id', '')
    image_data = body.get('image_data', '')
    if not playlist_id or not image_data or not _safe_media_component(playlist_id):
        return JSONResponse({"error": "Нет данных"}, status_code=400)
    try:
        img_bytes = base64.b64decode(image_data.split(',')[1] if ',' in image_data else image_data)
    except Exception:
        return JSONResponse({"error": "Некорректные данные изображения"}, status_code=400)
    if not img_bytes or len(img_bytes) > 8 * 1024 * 1024:
        return JSONResponse({"error": "Файл слишком большой"}, status_code=413)
    result = await _run_sync(_playlist_cover_set, playlist_id, img_bytes,
                             profile_id=_profile_id_of(request))
    if not result:
        return JSONResponse({"error": "Плейлист не найден или недоступен"}, status_code=404)
    return {"ok": True, "playlist": result}


@router.post("/api/media/playlist/cover/clear")
async def api_media_playlist_cover_clear(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    playlist_id = body.get('playlist_id', '')
    if not playlist_id or not _safe_media_component(playlist_id):
        return JSONResponse({"error": "Нет данных"}, status_code=400)
    result = await _run_sync(_playlist_cover_clear, playlist_id,
                             profile_id=_profile_id_of(request))
    if not result:
        return JSONResponse({"error": "Плейлист не найден или недоступен"}, status_code=404)
    return {"ok": True, "playlist": result}

@router.post("/api/media/lyrics/save")
async def api_media_lyrics_save(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await _run_sync(server.save_lyrics, body.get('track_id', ''), body.get('lyrics_data', {}))
    return {"ok": result}

@router.post("/api/media/lyrics/fetch")
async def api_media_lyrics_fetch(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    lyrics = await _run_sync(server.fetch_lyrics_from_internet, body.get('artist', ''), body.get('title', ''))
    return {"lyrics": lyrics}

@router.post("/api/media/lyrics/recognize")
async def api_media_lyrics_recognize(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    track_id = body.get('track_id', '')
    language = body.get('language') or None
    if not track_id:
        return JSONResponse({"error": "track_id обязателен"}, status_code=400)
    filepath = await _run_sync(server.get_track_filepath, track_id)
    if not filepath:
        return JSONResponse({"error": "Трек не найден"}, status_code=404)
    if not os.path.exists(filepath):
        return JSONResponse({"error": "Файл трека не найден"}, status_code=404)
    try:
        import speech_engine
        with open(filepath, 'rb') as f:
            audio = f.read()
        result = await worker_pool.run_async(
            speech_engine.transcribe_stt, audio, language=language)
        text = (result.get('text') or '').strip()
        if not text:
            return JSONResponse({"error": "Распознавание не дало результата"}, status_code=400)
        await _run_sync(server.save_lyrics, track_id, text, 'stt')
        return {"ok": True, "text": text}
    except Exception as e:
        logger.error(f"/api/media/lyrics/recognize error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/media/artist-info/save")
async def api_media_artist_info_save(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    result = await _run_sync(server.save_artist_info, body.get('artist_name', ''), body.get('info_data', {}))
    return {"ok": result}

@router.post("/api/media/movie-metadata/save")
async def api_media_movie_metadata_save(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    video_id = body.get('video_id', '')
    if not video_id:
        return JSONResponse({"error": "Нет video_id"}, status_code=400)

    def _save():
        import media_module
        conn = media_module.get_db()
        try:
            row = conn.execute("SELECT id FROM video_files WHERE id = ?", (video_id,)).fetchone()
            if not row:
                return False
            fields, params = [], []
            for key in ('overview', 'tagline', 'director'):
                if key in body:
                    fields.append(f"{key} = ?")
                    params.append(body.get(key) or '')
            if 'year' in body:
                year = body.get('year')
                if year in (None, ''):
                    year = None
                else:
                    try:
                        year = int(str(year)[:4])
                    except (TypeError, ValueError):
                        swallow.note('legacy_media_player._save:L1919')
                        year = None
                fields.append("year = ?")
                params.append(year)
            for key in ('genres', 'actors'):
                if key in body:
                    value = body.get(key) or []
                    if not isinstance(value, list):
                        value = [value]
                    fields.append(f"{key} = ?")
                    params.append(json.dumps(value, ensure_ascii=False))
            if fields:
                params.append(video_id)
                conn.execute(
                    f"UPDATE video_files SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                conn.commit()
            return True
        finally:
            conn.close()

    ok = await worker_pool.run_async(_save)
    if not ok:
        return JSONResponse({"error": "Видео не найдено"}, status_code=404)
    _cache_invalidate("video_index")
    return {"ok": True}

@router.post("/api/media/video-poster/upload")
async def api_media_video_poster_upload(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    video_id = body.get('video_id', '')
    image_data = body.get('image_data', '')
    image_type = body.get('image_type', 'poster')
    if not video_id or not image_data or not _safe_media_component(video_id):
        return JSONResponse({"error": "Нет данных"}, status_code=400)
    try:
        img_bytes = base64.b64decode(image_data.split(',')[1] if ',' in image_data else image_data)
        if image_type == 'poster':
            save_path = os.path.join(server.POSTERS_DIR, f"{video_id}_poster.jpg")
        else:
            save_path = os.path.join(server.BACKDROPS_DIR, f"{video_id}_still_{int(time.time())}.jpg")
        await worker_pool.write_file(save_path, img_bytes, mode="wb")

        def _update_db():
            import media_module
            conn = media_module.get_db()
            try:
                row = conn.execute("SELECT id, stills FROM video_files WHERE id = ?", (video_id,)).fetchone()
                if row:
                    if image_type == 'poster':
                        conn.execute(
                            "UPDATE video_files SET poster_url = ?, poster_path = ?, has_poster = 1 WHERE id = ?",
                            (f"/api/media/poster/{video_id}", save_path, video_id),
                        )
                    else:
                        try:
                            stills = json.loads(row['stills']) if row['stills'] else []
                        except Exception:
                            swallow.note('legacy_media_player._update_db:L1980')
                            stills = []
                        stills.append(f"/api/media/still/{os.path.basename(save_path)}")
                        conn.execute(
                            "UPDATE video_files SET stills = ? WHERE id = ?",
                            (json.dumps(stills, ensure_ascii=False), video_id),
                        )
                    conn.commit()
                    return True
                # Обложка сериала: клиент передаёт имя сериала вместо id видео.
                if image_type == 'poster':
                    conn.execute(
                        "INSERT OR IGNORE INTO series_meta (name) VALUES (?)", (video_id,)
                    )
                    conn.execute(
                        "UPDATE series_meta SET poster_url = ? WHERE name = ?",
                        (f"/api/media/poster/{video_id}", video_id),
                    )
                    conn.commit()
                    return True
                return False
            finally:
                conn.close()

        updated = await worker_pool.run_async(_update_db)
        if updated:
            _cache_invalidate("video_index")
        return {"ok": True, "path": save_path, "linked": bool(updated)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/media/still/{still_name:path}")
async def api_media_still(still_name: str):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    if not _safe_media_component(still_name):
        return JSONResponse({"error": "Still not found"}, status_code=404)
    still_path = os.path.join(server.BACKDROPS_DIR, still_name)
    if os.path.exists(still_path):
        data = await worker_pool.read_file(still_path, mode="rb")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({"error": "Still not found"}, status_code=404)

@router.post("/api/media/track-lyrics/save")
async def api_media_track_lyrics_save(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    track_id = body.get('track_id', '')
    lyrics_text = body.get('lyrics', '')
    if not track_id or not lyrics_text or not _safe_media_component(track_id):
        return JSONResponse({"error": "Нет данных"}, status_code=400)
    lyrics_data = {'track_id': track_id, 'lyrics': lyrics_text, 'source': 'manual', 'synced': False, 'lines': []}
    result = server.save_lyrics(track_id, lyrics_data)
    midx = await _get_cached_music_index(_profile_id_of(request))
    for track in midx.get('tracks', []):
        if track['id'] == track_id:
            track['has_lyrics'] = True
            break
    return {"ok": result}

@router.post("/api/media/track-cover/upload")
async def api_media_track_cover_upload(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    track_id = body.get('track_id', '')
    image_data = body.get('image_data', '')
    if not track_id or not image_data or not _safe_media_component(track_id):
        return JSONResponse({"error": "Нет данных"}, status_code=400)
    try:
        img_bytes = base64.b64decode(image_data.split(',')[1] if ',' in image_data else image_data)
        save_path = await _run_sync(server._save_track_cover, track_id, img_bytes, 'jpg')
        if save_path:
            midx = await _get_cached_music_index(_profile_id_of(request))
            for track in midx.get('tracks', []):
                if track['id'] == track_id:
                    track['has_cover'] = True
                    track['cover_path'] = save_path
                    break
            return {"ok": True, "path": save_path}
        return JSONResponse({"error": "Failed to save cover"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/media/validate-all")
async def api_media_validate_all():
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})

    def _validate():
        import media_module
        conn = media_module.get_db()
        validated = 0
        corrupted = 0
        try:
            rows = conn.execute("SELECT id, filepath FROM video_files").fetchall()
            for row in rows:
                filepath = row['filepath']
                if not filepath or not os.path.exists(filepath):
                    conn.execute(
                        "UPDATE video_files SET is_valid = 0, validation_error = ? WHERE id = ?",
                        ('Файл не найден', row['id']),
                    )
                    corrupted += 1
                    continue
                health = server.check_video_file_health(filepath)
                if not health.get('valid'):
                    conn.execute(
                        "UPDATE video_files SET is_valid = 0, validation_error = ? WHERE id = ?",
                        (health.get('error') or 'Ошибка проверки', row['id']),
                    )
                    corrupted += 1
                    continue
                fields = ["is_valid = 1", "validation_error = ?"]
                params = ['']
                for key in ('width', 'height', 'video_codec', 'duration'):
                    value = health.get(key)
                    if value not in (None, ''):
                        fields.append(f"{key} = ?")
                        params.append(value)
                audio_codecs = health.get('audio_codecs') or []
                if audio_codecs:
                    fields.append("audio_codec = ?")
                    params.append(audio_codecs[0])
                try:
                    fields.append("file_size = ?")
                    params.append(os.path.getsize(filepath))
                except OSError:
                    swallow.note('legacy_media_player._validate:L2110')
                    pass
                params.append(row['id'])
                conn.execute(
                    f"UPDATE video_files SET {', '.join(fields)} WHERE id = ?",
                    params,
                )
                validated += 1
            conn.commit()
        finally:
            conn.close()
        _cache_invalidate("video_index")
        return {"ok": True, "validated": validated, "corrupted": corrupted}

    result = await worker_pool.run_async(_validate)
    return result

@router.post("/api/media/start-indexing")
async def api_media_start_indexing(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    music_dirs = body.get('music_dirs', [])
    video_dirs = body.get('video_dirs', [])
    storage = body.get('storage', os.path.expanduser("~/.infinity/media"))
    def run_indexer():
        server.INDEX_PROGRESS['active'] = True
        server.INDEX_PROGRESS['message'] = 'Запуск индексации...'
        server.INDEX_PROGRESS['current'] = 0
        server.INDEX_PROGRESS['total'] = 100
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media_indexer.py')]
        if music_dirs:
            cmd.extend(['--music'] + music_dirs)
        if video_dirs:
            cmd.extend(['--video'] + video_dirs)
        cmd.extend(['--storage', storage])
        logger.info(f"Запуск индексации: {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            server.INDEX_PROGRESS['message'] = proc.stdout[-500:] if proc.stdout else 'Завершено'
            server.INDEX_PROGRESS['active'] = False
            logger.info(f"Индексация завершена: {proc.returncode}")
        except Exception as e:
            server.INDEX_PROGRESS['message'] = f"Ошибка: {e}"
            server.INDEX_PROGRESS['active'] = False
            logger.error(f"Индексация ошибка: {e}")
    worker_pool.background_task(run_indexer)
    return {"ok": True, "message": "Индексация запущена"}

@router.post("/api/media/play-log")
async def api_media_play_log(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_media_player.api_media_play_log:L2166')
        body = {}
    track_id = (body.get('id') or body.get('track_id')) if isinstance(body, dict) else None
    if not track_id:
        return JSONResponse({"error": "Нет id трека"}, status_code=400)
    # История общая на процесс (в таблице нет профиля) и по ней мини-плеер
    # выбирает «последний прослушанный» трек, а рекомендации — интересы.
    # Скрытый для профиля трек не должен туда попадать.
    filepath = await worker_pool.run_async(_media_filepath, "music_tracks", track_id)
    if filepath and not _visible_to_profile(filepath, _profile_id_of(request)):
        return JSONResponse({"error": "Трек недоступен"}, status_code=403)
    try:
        duration = float(body.get('duration') or 0)
    except (TypeError, ValueError):
        swallow.note('legacy_media_player.api_media_play_log:L2174')
        duration = 0
    from media_module import log_play_server
    await worker_pool.run_async(log_play_server, track_id, duration)
    return {"ok": True}


@router.post("/api/media/watch-log")
async def api_media_watch_log(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    try:
        body = await request.json()
    except Exception:
        swallow.note('legacy_media_player.api_media_watch_log:L2188')
        body = {}
    video_id = (body.get('id') or body.get('video_id')) if isinstance(body, dict) else None
    if not video_id:
        return JSONResponse({"error": "Нет id видео"}, status_code=400)
    # Та же причина, что у play-log: история просмотров одна на процесс.
    filepath = await worker_pool.run_async(_media_filepath, "video_files", video_id)
    if filepath and not _visible_to_profile(filepath, _profile_id_of(request)):
        return JSONResponse({"error": "Видео недоступно"}, status_code=403)
    try:
        watched = float(body.get('duration_watched') or 0)
        total = float(body.get('total_duration') or 0)
    except (TypeError, ValueError):
        swallow.note('legacy_media_player.api_media_watch_log:L2197')
        watched, total = 0, 0
    from media_module import log_watch_server
    await worker_pool.run_async(log_watch_server, video_id, watched, total)
    return {"ok": True}


@router.post("/api/media/delete-music")
async def api_media_delete_music():
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    try:
        import media_module
        await worker_pool.run_async(media_module.clear_music_index)
        _cache_invalidate("music_index")
        return {"ok": True, "message": "Индекс музыки удалён"}
    except Exception as e:
        logger.error(f"Ошибка удаления индекса музыки: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/media/delete-video")
async def api_media_delete_video():
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    try:
        import media_module
        await worker_pool.run_async(media_module.clear_video_index)
        _cache_invalidate("video_index")
        return {"ok": True, "message": "Индекс видео удалён"}
    except Exception as e:
        logger.error(f"Ошибка удаления индекса видео: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/media/series/save")
async def api_media_series_save(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    name = (body.get('name') or '').strip() if isinstance(body, dict) else ''
    if not name:
        return JSONResponse({"error": "Нет названия сериала"}, status_code=400)

    def _save():
        import media_module
        media_module.save_series_server(name, {
            'overview': body.get('overview', ''),
            'genres': body.get('genres', []),
            'first_air_date': body.get('first_air_date', ''),
            'poster_url': body.get('poster_url', ''),
        })
        episode_ids = body.get('episode_ids') or []
        if episode_ids:
            conn = media_module.get_db()
            try:
                fields = []
                params = []
                if 'overview' in body:
                    fields.append("overview=?")
                    params.append(body.get('overview') or '')
                if 'director' in body:
                    fields.append("director=?")
                    params.append(body.get('director') or '')
                if 'genres' in body:
                    fields.append("genres=?")
                    params.append(json.dumps(body.get('genres') or []))
                if 'actors' in body:
                    fields.append("actors=?")
                    params.append(json.dumps(body.get('actors') or []))
                if 'first_air_date' in body:
                    year = str(body.get('first_air_date') or '')[:4]
                    fields.append("year=?")
                    params.append(int(year) if year.isdigit() else None)
                if fields:
                    placeholders = ",".join("?" for _ in episode_ids)
                    conn.execute(
                        f"UPDATE video_files SET {', '.join(fields)} WHERE id IN ({placeholders})",
                        params + list(episode_ids),
                    )
                    conn.commit()
            finally:
                conn.close()
        return True

    await worker_pool.run_async(_save)
    _cache_invalidate("video_index")
    return {"ok": True}


@router.post("/api/media/collections/create")
async def api_media_collections_create(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    name = body.get('name', 'Новая коллекция')
    description = body.get('description', '')
    items = body.get('items') or []
    from media_module import create_collection_direct
    result = await worker_pool.run_async(create_collection_direct, name, description, items)
    return {"ok": True, "id": result.get('id', ''), "collection": result}


@router.post("/api/media/collections/add")
async def api_media_collections_add(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    collection_id = body.get('collection_id') if isinstance(body, dict) else None
    video_id = (body.get('video_id') or body.get('id')) if isinstance(body, dict) else None
    if not collection_id or not video_id:
        return JSONResponse({"error": "Нужны collection_id и video_id"}, status_code=400)
    from media_module import add_to_collection_direct
    ok = await worker_pool.run_async(add_to_collection_direct, collection_id, video_id)
    if not ok:
        return JSONResponse({"error": "Коллекция не найдена"}, status_code=404)
    return {"ok": True}


@router.post("/api/media/collections/remove")
async def api_media_collections_remove(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    collection_id = body.get('collection_id') if isinstance(body, dict) else None
    video_id = (body.get('video_id') or body.get('id')) if isinstance(body, dict) else None
    if not collection_id or not video_id:
        return JSONResponse({"error": "Нужны collection_id и video_id"}, status_code=400)
    from media_module import remove_from_collection_direct
    ok = await worker_pool.run_async(remove_from_collection_direct, collection_id, video_id)
    if not ok:
        return JSONResponse({"error": "Коллекция не найдена"}, status_code=404)
    return {"ok": True}


@router.post("/api/media/collections/delete")
async def api_media_collections_delete(request: Request):
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    body = await request.json()
    collection_id = (body.get('id') or body.get('collection_id')) if isinstance(body, dict) else None
    if not collection_id:
        return JSONResponse({"error": "Нет id коллекции"}, status_code=400)
    from media_module import delete_collection_direct
    await worker_pool.run_async(delete_collection_direct, collection_id)
    return {"ok": True}

@router.post("/api/media/convert-avi")
def api_media_convert_avi():
    """Перекодировать все видео, которые браузер не проигрывает (AVI/MKV и т.п.)."""
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    if server.INDEX_PROGRESS.get('active'):
        return JSONResponse({"error": "Другая операция с медиатекой уже выполняется"}, status_code=409)

    def _run():
        import media_module
        converted = 0
        failures = 0
        try:
            conn = media_module.get_db()
            try:
                rows = conn.execute(
                    "SELECT id, filepath FROM video_files WHERE is_valid = 1"
                ).fetchall()
            finally:
                conn.close()
            todo = []
            for row in rows:
                src = row['filepath']
                if not src or not os.path.exists(src):
                    continue
                try:
                    if not transcoder.needs_transcoding(src):
                        continue
                except Exception:
                    swallow.note('legacy_media_player._run:L2373')
                    continue
                dst = src.rsplit('.', 1)[0] + '.mp4'
                if os.path.exists(dst):
                    continue
                todo.append((row['id'], src, dst))
            total = len(todo)
            server.INDEX_PROGRESS['total'] = total
            server.INDEX_PROGRESS['current'] = 0
            if not total:
                server.INDEX_PROGRESS['message'] = 'Нет файлов, требующих перекодирования'
                return
            for i, (vid, src, dst) in enumerate(todo, 1):
                server.INDEX_PROGRESS['message'] = (
                    f'Перекодирование {i}/{total}: {os.path.basename(src)}'
                )
                try:
                    ok = transcoder.do_transcode(src, dst)
                except Exception as e:
                    logger.warning(f"convert-avi: {src}: {e}")
                    ok = False
                if ok and os.path.exists(dst):
                    conn = media_module.get_db()
                    try:
                        conn.execute("UPDATE video_files SET filepath = ? WHERE id = ?", (dst, vid))
                        conn.commit()
                    finally:
                        conn.close()
                    converted += 1
                    _cache_invalidate("video_index")
                else:
                    failures += 1
                server.INDEX_PROGRESS['current'] = i
            msg = f'Готово: перекодировано {converted} из {total}'
            if failures:
                msg += f', ошибок {failures}'
            server.INDEX_PROGRESS['message'] = msg
        except Exception as e:
            logger.error(f"convert-avi error: {e}", exc_info=True)
            server.INDEX_PROGRESS['message'] = f'Ошибка: {e}'
        finally:
            server.INDEX_PROGRESS['active'] = False

    server.INDEX_PROGRESS.update({
        'active': True,
        'message': 'Подготовка списка файлов...',
        'current': 0,
        'total': 0,
    })
    worker_pool.background_task(_run)
    return {"ok": True, "started": True, "converted": 0,
            "message": "Перекодирование запущено в фоне"}


async def _media_file_response(path: str, request: Request, filename: str = ""):
    """Отдать файл медиатеки как загрузку — с учётом шифрования.

    «Скачать» раньше проверял os.path.exists(исходного пути) и стримил его же.
    Для зашифрованной библиотеки в БД лежит исходный путь, а на диске только
    контейнер <путь>.infenc, поэтому кнопка у фильма возвращала 404, а у
    оставшегося plaintext отдавала не тот файл. Разбор пути здесь тот же, что у
    плеера: зашифрованное идёт через _stream_encrypted и приезжает plaintext'ом.
    """
    from modules import media_transfer as MT
    source, encrypted = _resolve_media_source(path)
    if not source:
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    media_type = _media_content_type(path)
    if encrypted:
        # _stream_encrypted ждёт путь plaintext-логина: контейнер он дописывает сам
        resp = _stream_encrypted(path, request, media_type)
    else:
        resp = await _stream_file_range(source, request, media_type)
    if getattr(resp, "status_code", 200) < 400:
        resp.headers["Content-Disposition"] = MT.attachment_header(
            filename or os.path.basename(path))
    return resp


@router.get("/api/media/music-download")
async def api_media_music_download(request: Request):
    """Скачать один трек.

    Берём из индекса профиля, а не из таблицы: выдача индекса уже отфильтрована
    по видимым каталогам, поэтому «нет в индексе» = «профиль не видит», и путь
    из запроса не превращается в обход чужой приватной библиотеки.
    """
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    tid = request.query_params.get("id", "")
    idx = await _get_cached_music_index(_profile_id_of(request))
    track = next((t for t in idx.get('tracks', []) if str(t.get('id')) == str(tid)), None)
    if not track or not track.get('filepath'):
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    fp = track['filepath']
    # имя только из basename: путь «альбом/трек» в Content-Disposition браузер
    # превращает в подкаталог, а одиночная загрузка — всегда один файл
    return await _media_file_response(fp, request, os.path.basename(fp))


@router.post("/api/media/download-zip")
async def api_media_download_zip(request: Request):
    """Набор одним архивом: альбом, плейлист, все серии, подборка фото.

    Клиент присылает список id (решение пользователя), сервер сам находит пути —
    иначе кнопка «скачать всё» была бы обёрткой над N запросами и рвала бы
    соединение на каждом мобильном интернете. Архив store-only и стримится:
    трек уже сжат, повторная упаковка платит только CPU и ничем не помогает, а
    буферизовать весь набор в память было бы нельзя.
    """
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"})
    from modules import media_transfer as MT
    kind, ids, name = await _zip_request_args(request)
    if kind not in ('music', 'video', 'photos') or not ids:
        return JSONResponse({"error": "Нужен kind (music/video/photos) и список ids"},
                            status_code=400)
    if len(ids) > MT.MAX_SET_ITEMS:
        return JSONResponse({"error": f"Слишком большой набор: до {MT.MAX_SET_ITEMS} файлов"},
                            status_code=413)
    pairs = await _zip_pairs(kind, ids, _profile_id_of(request))
    if not pairs:
        return JSONResponse({"error": "Ничего доступного этому профилю не найдено"},
                            status_code=404)
    entries = MT.zip_entries(pairs)
    filename = f"{MT.safe_component(name) or kind}.zip"
    return StreamingResponse(
        MT.zip_stream_async(entries),
        media_type="application/zip",
        headers={"Content-Disposition": MT.attachment_header(filename),
                 "Cache-Control": "no-cache",
                 # размер неизвестен: он считается на ходу, пока идут данные
                 "Accept-Ranges": "none"})


async def _zip_request_args(request: Request):
    """(kind, ids, имя архива) из JSON или формы.

    Форму шлёт скрытый <form target=_blank> — у fetch'а нет способа отдать
    большой браузерный download без чтения всего тела в JS, а форма не умеет
    ни JSON, ни вложенные массивы, поэтому ids приходят одной строкой.
    """
    ctype = (request.headers.get('content-type') or '').split(';')[0].strip()
    if ctype == 'application/json':
        body = await request.json()
        raw_ids = body.get('ids') or body.get('id') or []
        if isinstance(raw_ids, str):
            raw_ids = raw_ids.split(',')
        return (str(body.get('kind') or 'music'),
                [str(i) for i in raw_ids if str(i).strip()],
                str(body.get('name') or ''))
    form = await request.form()
    raw = str(form.get('ids') or '')
    return (str(form.get('kind') or 'music'),
            [x.strip() for x in raw.replace(';', ',').split(',') if x.strip()],
            str(form.get('name') or ''))


async def _zip_pairs(kind, ids, profile_id):
    """[(имя_в_архиве, путь, зашифрован_ли)] для запрошенных id этого профиля."""
    rows = []
    if kind == 'music':
        idx = await _get_cached_music_index(profile_id)
        by_id = {str(t.get('id')): t for t in idx.get('tracks', [])}
        rows = [by_id.get(i) for i in ids if by_id.get(i)]
    elif kind == 'video':
        idx = await _get_cached_video_index(profile_id)
        by_id = {str(v.get('id')): v for v in idx.get('videos', [])}
        rows = [by_id.get(i) for i in ids if by_id.get(i)]
    else:
        if not server.PHOTO_AVAILABLE:
            return []
        import media_visibility as MV
        for i in ids:
            photo = await worker_pool.run_async(server.PHOTO.get_photo, i, profile_id)
            if not photo or not photo.get('filepath'):
                continue
            if not MV.stream_allowed(photo['filepath'], profile_id):
                continue
            rows.append(photo)

    from modules import media_transfer as MT
    out = []
    taken = set()
    for row in rows:
        fp = row.get('filepath') or ''
        source, encrypted = _resolve_media_source(fp)
        if not source:
            continue
        base = row.get('filename') or os.path.basename(fp)
        name = MT.unique_arcname(taken, base)
        taken.add(name)
        out.append((name, fp, encrypted))
    return out


# ---------- «Загрузить»: файл, папка или ZIP в библиотеку ----------

def _upload_guard():
    """Отказ, если приём файлов в медиатеку выключен, иначе None.

    Рубильник проверяется в маршруте, а не только в UI: кнопка скрыта, а POST на
    прямой путь жив — ровно тот класс дыры, что закрывали в «Скачать»-наборе.
    """
    from modules import media_transfer as MT
    if not MT.upload_enabled():
        return JSONResponse({"error": "Загрузка в медиатеку выключена — включите её в настройках"},
                            status_code=403)
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"}, status_code=503)
    return None


@router.post("/api/media/upload/begin")
async def api_media_upload_begin(request: Request):
    """Открыть сессию загрузки: имя, категория, обещанный размер.

    size берётся из File.size и сверяется при finish — по нему же клиент режет
    файл на чанки, поэтому недогруз становится ошибкой, а не тихим усечённым
    файлом в библиотеке.
    """
    err = _upload_guard()
    if err is not None:
        return err
    from modules import media_transfer as MT
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Нужен JSON: category, filename, size"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Нужен JSON-объект"}, status_code=400)
    try:
        return await worker_pool.run_async(
            MT.begin, str(body.get('category') or ''), _profile_id_of(request),
            str(body.get('filename') or ''), str(body.get('relpath') or ''),
            body.get('size'))
    except MT.TransferError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@router.put("/api/media/upload/{sid}")
async def api_media_upload_chunk(sid: str, request: Request):
    """Один чанк сырыми байтами в тело запроса.

    multipart тут не вариант: граница и заголовки части осели бы в .part, а
    разбор формы копировал бы гигабайты. Чанк меньше лимита тела security_guard,
    поэтому фильм проходит, не поднимая глобальный max_body_mb.
    """
    err = _upload_guard()
    if err is not None:
        return err
    from modules import media_transfer as MT
    data = await request.body()
    try:
        return await worker_pool.run_async(MT.append, sid, data)
    except MT.TransferError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@router.post("/api/media/upload/{sid}/finish")
async def api_media_upload_finish(sid: str, request: Request):
    """Дописать готовое в библиотеку и запустить переиндексацию каталога."""
    err = _upload_guard()
    if err is not None:
        return err
    from modules import media_transfer as MT
    try:
        res = await worker_pool.run_async(MT.finish, sid)
    except MT.TransferError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    _rescan_after_upload(res.get('category'), res.get('target'))
    res['rescan'] = True
    return res


@router.post("/api/media/upload/{sid}/abort")
async def api_media_upload_abort(sid: str, request: Request):
    err = _upload_guard()
    if err is not None:
        return err
    from modules import media_transfer as MT
    return await worker_pool.run_async(MT.abort, sid)


def _run_media_rescan(category, target):
    """Один проход индексации целевого каталога — в фоновом пуле."""
    def _run():
        try:
            if category == 'music':
                server.rebuild_music_index([target])
                _cache_invalidate('music_index')
            elif category == 'video':
                server.rebuild_video_index([target])
                _cache_invalidate('video_index')
            elif category == 'photos' and server.PHOTO_AVAILABLE:
                server.PHOTO.scan_photos_direct([target])
        except Exception:
            logger.exception("Загрузка: переиндексация %s не удалась", category)

    try:
        worker_pool.background_task(_run)
    except Exception:
        logger.exception("Загрузка: переиндексация не запущена (%s)", category)


_RESCAN_DELAY = 4.0
_rescan_timers = {}
_rescan_lock = threading.Lock()


def _rescan_after_upload(category, target):
    """Отложенный и сливаемый: папка из 40 файлов = один проход, а не сорок.

    Файл на диске — ещё не библиотека: без индекса его никто не увидит. Но finish
    вызывается на каждый файл, а каждый проход — это обход каталога и запись в
    медиатему; сорок подряд идут транзакциями внутрь одной базы и упираются в
    тот же «database is locked», что уже лечили промежуточными коммитами. Таймер
    переставляется на каждом новом файле, поэтому прогон стартует один раз —
    через RESCAN_DELAY после последнего.

    Скоуп — только целевой каталог: scope очистки неактивных директорий берёт
    объединение каталогов всех профилей (media_module._prune_prefixes), а не
    список сканирования, поэтому чужие строки не пострадают.
    """
    if not category or not target:
        return
    key = (category, target)
    with _rescan_lock:
        old = _rescan_timers.pop(key, None)
        if old is not None:
            old.cancel()
        timer = threading.Timer(_RESCAN_DELAY, _fire_rescan, args=key)
        timer.daemon = True
        _rescan_timers[key] = timer
        timer.start()


def _fire_rescan(category, target):
    """Дозвон таймера: снять себя с расписания и отдать работу пулу."""
    with _rescan_lock:
        _rescan_timers.pop((category, target), None)
    _run_media_rescan(category, target)


# ---------- «Чистка битого»: отсутствующие, нулевые и нечитаемые файлы ----------

def _autoclean_guard(pid):
    """Отказ, если чистка выключена у этого профиля, иначе None.

    Рубильник проверяется в маршруте, а не только в UI: скрытая кнопка не
    защищает от POST на прямой путь — тот же класс дыры, что закрывали в
    «Скачать» и «Загрузить». Судит флаг профиля, а не общий ключ настроек:
    иначе включение у одного открывало разрушающее удаление всем.
    """
    import media_autoclean as AC
    if not AC.autoclean_enabled(pid):
        return JSONResponse({"error": "Чистка битого выключена — включите её в настройках"},
                            status_code=403)
    if not server.MEDIA_AVAILABLE:
        return JSONResponse({"error": "Медиа модуль недоступен"}, status_code=503)
    return None


def _flag_pid(request):
    """Профиль, чей флаг модуля читаем и пишем.

    module_flags хранит права по профилю, а запрос без куки — это
    общедоступный профиль: записывать флаг пустому ключу нельзя, поэтому
    берётся его идентификатор.
    """
    pid = _profile_id_of(request)
    if pid:
        return pid
    try:
        import profiles_module as PM
        return PM.get_default_profile_id()
    except Exception:
        logger.exception("Флаг модуля: профиль запроса не определён")
        return ""


def _autoclean_pid(request):
    return _flag_pid(request)


async def _autoclean_start(request, apply_changes):
    pid = _autoclean_pid(request)
    err = _autoclean_guard(pid)
    if err is not None:
        return (None, err)
    import media_autoclean as AC
    deep = True
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict) and body.get("deep") is not None:
        deep = bool(body.get("deep"))
    task_id, job_err = AC.start(pid, apply_changes=apply_changes, deep=deep)
    if job_err is not None:
        return (None, JSONResponse(job_err, status_code=409))
    return (task_id, None)


@router.get("/api/media/autoclean/state")
async def api_media_autoclean_state(request: Request):
    import media_autoclean as AC
    pid = _autoclean_pid(request)
    task_id = AC.active_task(pid)
    job = AC.job(task_id, pid) if task_id else None
    try:
        ignored = AC.ignored_summary(pid)
    except Exception as e:
        logger.warning("Чистка битого: память приговоров не прочитана: %s", e)
        ignored = {"total": 0}
    return {"ok": True, "enabled": AC.autoclean_enabled(pid), "running": bool(task_id),
            "task_id": task_id, "job": job, "ignored": ignored}


@router.post("/api/media/autoclean/flag")
async def api_media_autoclean_flag(request: Request):
    """Переключатель чистки в настройках медиа = флаг модуля этого профиля.

    Раньше чекбокс писал общий ключ в таблице settings, и один щелчок включал
    разрушающую кнопку всем профилям. Право здесь — только на свой профиль:
    «включить всем» живёт в админке и требует админ-токена.
    """
    import media_autoclean as AC
    import module_flags
    pid = _autoclean_pid(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or "enabled" not in body:
        return JSONResponse({"error": "Нужно поле enabled"}, status_code=400)
    try:
        module_flags.set_flag_for(AC.MODULE, "enabled", bool(body.get("enabled")), pid)
    except Exception as e:
        logger.warning("Чистка битого: флаг профиля %s не записан: %s", pid, e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "enabled": AC.autoclean_enabled(pid), "profile": pid}


@router.post("/api/media/autoclean/forget")
async def api_media_autoclean_forget(request: Request):
    """Забыть приговоры: следующий скан снова разберёт эти файлы.

    Разрушением не является — снимаются только записи памяти чистки, строки
    индекса и байты не трогаются. Нужно, когда файл починили, сохранив размер
    и время изменения (тогда отпечаток совпадает со старым приговором).
    """
    import media_autoclean as AC
    pid = _autoclean_pid(request)
    err = _autoclean_guard(pid)
    if err is not None:
        return err
    try:
        cleared = AC.forget_ignored(pid)
    except Exception as e:
        logger.warning("Чистка битого: память приговоров не очищена: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "cleared": cleared, "ignored": AC.ignored_summary(pid)}


@router.post("/api/media/autoclean/count")
async def api_media_autoclean_count(request: Request):
    """Только посчитать: ничего не удаляется, отчёт тот же."""
    task_id, err = await _autoclean_start(request, apply_changes=False)
    if err is not None:
        return err
    return {"ok": True, "task_id": task_id}


@router.post("/api/media/autoclean/run")
async def api_media_autoclean_run(request: Request):
    """Нашла → удалила → отчёт. Файлы на диске остаются, уходят строки индекса."""
    task_id, err = await _autoclean_start(request, apply_changes=True)
    if err is not None:
        return err
    return {"ok": True, "task_id": task_id}


@router.get("/api/media/autoclean/job/{task_id}")
async def api_media_autoclean_job(task_id: str, request: Request):
    """Прогресс и отчёт — только тому профилю, который задачу запустил."""
    import media_autoclean as AC
    job = AC.job(task_id, _autoclean_pid(request))
    if job is None:
        return JSONResponse({"error": "Задача не найдена"}, status_code=404)
    return {"ok": True, "job": job}
