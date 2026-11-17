import sys, os, json, logging

import server_backup as _original
import swallow

logger = _original.logger

# --- Media config constants ---
PHOTOS_DIR = _original.PHOTOS_DIR
MEDIA_CFG = _original.MEDIA_CFG
MUSIC_DIRS = _original.MUSIC_DIRS
VIDEO_DIRS = _original.VIDEO_DIRS

ALLOWED_IMAGE_EXT = _original.ALLOWED_IMAGE_EXT
ALLOWED_AUDIO_EXT = _original.ALLOWED_AUDIO_EXT
ALLOWED_VIDEO_EXT = _original.ALLOWED_VIDEO_EXT
ALLOWED_FILE_EXT = _original.ALLOWED_FILE_EXT

# --- Media module aliases ---
MEDIA_AVAILABLE = False
PAGE_MEDIA = ""
try:
    import media_module as MM
    scan_music_directories = MM.scan_music_directories
    scan_video_directories = MM.scan_video_directories
    rebuild_music_index = MM.rebuild_music_index
    rebuild_video_index = MM.rebuild_video_index
    clear_music_index = MM.clear_music_index
    clear_video_index = MM.clear_video_index
    sync_media_dirs_from_config = MM.sync_media_dirs_from_config
    get_music_index = MM.get_music_index
    get_video_index = MM.get_video_index
    group_tracks_by_artist = MM.group_tracks_by_artist
    group_tracks_by_album = MM.group_tracks_by_album
    group_tracks_by_genre = MM.group_tracks_by_genre
    search_tracks = MM.search_tracks
    get_lyrics = MM.get_lyrics
    save_lyrics = MM.save_lyrics
    fetch_lyrics_from_internet = MM.fetch_lyrics_from_internet
    get_artist_info = MM.get_artist_info
    save_artist_info = MM.save_artist_info
    get_artist_tracks = MM.get_artist_tracks
    get_artist_albums = MM.get_artist_albums
    get_favorites = MM.get_favorites
    add_to_favorites = MM.add_to_favorites
    remove_from_favorites = MM.remove_from_favorites
    is_favorite = MM.is_favorite
    get_playlists = MM.get_playlists
    create_playlist = MM.create_playlist
    get_playlist = MM.get_playlist
    update_playlist = MM.update_playlist
    add_track_to_playlist = MM.add_track_to_playlist
    remove_track_from_playlist = MM.remove_track_from_playlist
    delete_playlist = MM.delete_playlist
    INDEX_PROGRESS = MM.INDEX_PROGRESS
    find_duplicates = MM.find_duplicates
    get_recommendations = MM.get_recommendations
    _save_track_cover = MM._save_track_cover
    COVERS_DIR = os.path.join(MM.MEDIA_DIR, "covers")
    fetch_movie_metadata = MM.fetch_movie_metadata
    get_movie_metadata_cached = MM.get_movie_metadata_cached
    POSTERS_DIR = MM.POSTERS_DIR
    BACKDROPS_DIR = MM.BACKDROPS_DIR
    MOVIE_CACHE_FILE = MM.MOVIE_CACHE_FILE
    MEDIA_DIR = MM.MEDIA_DIR
    MUSIC_INDEX_FILE = MM.MUSIC_INDEX_FILE
    VIDEO_INDEX_FILE = MM.VIDEO_INDEX_FILE
    LYRICS_DIR = MM.LYRICS_DIR
    ARTISTS_DIR = MM.ARTISTS_DIR
    PLAYLISTS_DIR = MM.PLAYLISTS_DIR
    FAVORITES_FILE = MM.FAVORITES_FILE
    _save_json = MM._save_json
    check_video_file_health = MM.check_video_file_health
    get_media_statistics = MM.get_media_statistics
    find_random_video = MM.find_random_video
    convert_avi_videos = MM.convert_avi_videos
    start_transcode_server = MM.start_transcode_server
    save_series_server = MM.save_series_server
    delete_track_server = MM.delete_track_server
    log_play_server = MM.log_play_server
    log_watch_server = MM.log_watch_server
    add_to_collection_direct = MM.add_to_collection_direct
    remove_from_collection_direct = MM.remove_from_collection_direct
    delete_collection_direct = MM.delete_collection_direct
    PAGE_MEDIA = MM.PAGE_MEDIA
    MEDIA_AVAILABLE = True
    logger.info("Медиа модуль загружен")
except ImportError as e:
    logger.warning(f"Медиа модуль недоступен: {e}")

# --- Photo module aliases ---
PHOTO_AVAILABLE = False
PHOTO_DIRS = []
try:
    import photo_module as PHOTO
    _photo_cfg_dirs = _original.CFG.get("media", {}).get("photo_dirs", [])
    if _photo_cfg_dirs:
        PHOTO_DIRS = _photo_cfg_dirs
        PHOTO.set_photo_dirs_from_config(_photo_cfg_dirs)
    else:
        PHOTO_DIRS = PHOTO.get_photo_dirs()
    PHOTO_AVAILABLE = True
    logger.info("Photo модуль загружен")
except ImportError:
    swallow.note('media.<module>:L104')
    pass
except Exception as e:
    logger.warning(f"Photo модуль ошибка: {e}")

# --- GET route handlers ---

def _handle_media(self, p, q):
    if MEDIA_AVAILABLE:
        self._html(_original.PAGE_MEDIA)
    else:
        self._html("<h1>Медиа модуль недоступен</h1><p>Установите mutagen: pip install mutagen</p>")

def _handle_music_index(self, p, q):
    if MEDIA_AVAILABLE:
        try:
            limit = int(q.get("limit", [100])[0])
            offset = int(q.get("offset", [0])[0])
        except Exception:
            swallow.note('media._handle_music_index:L123')
            limit, offset = 100, 0
        genre = q.get("genre", [None])[0] or None
        self._json(get_music_index(limit=limit, offset=offset, genre=genre))
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_video_index(self, p, q):
    if MEDIA_AVAILABLE:
        self._json(get_video_index())
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_search(self, p, q):
    if MEDIA_AVAILABLE:
        query = q.get("q", [""])[0]
        index = get_music_index()
        results = search_tracks(query, index.get('tracks', []))
        self._json({"results": MM.fix_labels(results)})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_tracks_by_artist(self, p, q):
    if MEDIA_AVAILABLE:
        artist = q.get("artist", [""])[0]
        index = get_music_index()
        tracks = [t for t in index.get('tracks', []) if t.get('artist') == artist]
        self._json({"tracks": tracks})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_tracks_by_album(self, p, q):
    if MEDIA_AVAILABLE:
        album = q.get("album", [""])[0]
        index = get_music_index()
        tracks = [t for t in index.get('tracks', []) if t.get('album') == album]
        self._json({"tracks": tracks})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_lyrics(self, p, q):
    if MEDIA_AVAILABLE:
        track_id = q.get("track_id", [""])[0]
        # get_lyrics принимает id трека; раньше сюда передавали (artist, title)
        # и обработчик падал по несовпадению сигнатуры.
        self._json(get_lyrics(track_id))
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_artist_info(self, p, q):
    if MEDIA_AVAILABLE:
        artist = q.get("artist", [""])[0]
        self._json(get_artist_info(artist))
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_movie_metadata(self, p, q):
    if MEDIA_AVAILABLE:
        title = q.get("title", [""])[0]
        self._json(fetch_movie_metadata(title) or {})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_duplicates(self, p, q):
    if MEDIA_AVAILABLE:
        self._json({"duplicates": find_duplicates()})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_recommendations(self, p, q):
    if MEDIA_AVAILABLE:
        self._json({"recommendations": MM.fix_labels(get_recommendations())})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_favorites(self, p, q):
    if MEDIA_AVAILABLE:
        self._json({"favorites": get_favorites()})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_playlists(self, p, q):
    if MEDIA_AVAILABLE:
        self._json({"playlists": MM.fix_labels(get_playlists())})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_index_progress(self, p, q):
    if MEDIA_AVAILABLE:
        self._json({"progress": INDEX_PROGRESS})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_collections(self, p, q):
    if MEDIA_AVAILABLE:
        try:
            from modules.collection_module import get_collections
            collections = get_collections()
        except Exception:
            swallow.note('media._handle_collections:L222')
            collections = []
        self._json({"collections": collections})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

def _handle_favorites_videos(self, p, q):
    if MEDIA_AVAILABLE:
        favorites = get_favorites()
        videos = [f for f in favorites if f.get('type', '').startswith('video/')]
        self._json({"videos": videos})
    else:
        self._json({"error": "Медиа модуль недоступен"}, 503)

import logging
_media_logger = logging.getLogger("infinity")
