#!/usr/bin/env python3
"""
Индексация медиа — автономный скрипт.
Сканирует музыку/видео, скачивает тексты, обложки, метаданные TMDB/Last.fm
и сохраняет ВСЁ локально. После запуска основной сервер подтягивает данные
из локальных файлов без обращения к интернету.

Использование:
    python3 media_indexer.py --music /path/to/music --video /path/to/movies --storage /path/to/storage
"""

import os, sys, json, time, re, hashlib, argparse
from pathlib import Path
import swallow

# Добавляем путь к модулю
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===================== НАСТРОЙКИ =====================

AUDIO_EXTENSIONS = {'.mp3', '.flac', '.ogg', '.wav', '.aac', '.m4a', '.wma', '.opus'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.webm', '.mov', '.wmv', '.flv'}

# API ключи
LASTFM_API_KEY = "1f08b6f87e9b4da1a3911f55b7c3e0e2"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
TMDB_API_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJkMWQyYmE0ZjYxMTNjMjVlYjIzZjQwZjM3ZDc1NjQ0ZCIsIm5iZiI6MTcyMDAwMDAwMCwic3ViIjoiNjBhMmUzZjRmNjQ3YjUwMDJiMzQ1NjdlIiwic2NvcGVzIjpbImFwaV9yZWFkIl0sInZlcnNpb24iOjF9"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/"

# Mutagen для метаданных
MUTAGEN = False
try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC
    from mutagen.oggvorbis import OggVorbis
    from mutagen.mp4 import MP4
    from mutagen.id3 import ID3
    MUTAGEN = True
    print("✅ Mutagen загружен")
except ImportError:
    print("⚠️ Mutagen не установлен: pip install mutagen")

# ===================== ВСПОМОГАТЕЛЬНЫЕ =====================

def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_json(path, default=None):
    if default is None: default = {}
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        swallow.note('media_indexer._load_json:L58')
        pass
    return default

def _http_get_json(url, timeout=10):
    import urllib.request
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        swallow.note('media_indexer._http_get_json:L69', e)
        return None

def _http_get_bytes(url, timeout=15):
    import urllib.request
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        swallow.note('media_indexer._http_get_bytes:L79')
        return None

# ===================== ЧТЕНИЕ МЕТАДАННЫХ =====================

def read_audio_meta(filepath):
    """Чтение метаданных с извлечением текста и обложек."""
    meta = {'title':'','artist':'','album':'','genre':'','year':'','track_number':'',
            'duration':0,'bitrate':0,'sample_rate':0,'has_cover':False,'cover_data':None,'lyrics':''}
    if not MUTAGEN:
        # Базовое из имени файла
        fn = os.path.splitext(os.path.basename(filepath))[0]
        if ' - ' in fn:
            parts = fn.split(' - ', 1)
            if parts[0].strip().isdigit():
                meta['track_number'] = parts[0].strip()
                meta['title'] = parts[1].strip()
            else:
                meta['artist'] = parts[0].strip()
                meta['title'] = parts[1].strip()
        else:
            meta['title'] = fn
        return meta
    try:
        audio = mutagen.File(filepath)
        if not audio: return meta
        if hasattr(audio.info, 'length'): meta['duration'] = int(audio.info.length)
        if hasattr(audio.info, 'bitrate'): meta['bitrate'] = audio.info.bitrate
        if hasattr(audio.info, 'sample_rate'): meta['sample_rate'] = audio.info.sample_rate

        if isinstance(audio, MP3) and audio.tags:
            tags = audio.tags
            meta['title'] = str(tags.get("TIT2",""))
            meta['artist'] = str(tags.get("TPE1",""))
            meta['album'] = str(tags.get("TALB",""))
            meta['genre'] = str(tags.get("TCON",""))
            meta['year'] = str(tags.get("TDRC",""))
            meta['track_number'] = str(tags.get("TRCK",""))
            for fid, frame in tags.items():
                if fid.startswith('USLT') and not meta['lyrics']:
                    meta['lyrics'] = str(frame.text)
                elif fid.startswith('APIC') and not meta['cover_data']:
                    meta['has_cover'] = True
                    meta['cover_data'] = frame.data

        elif isinstance(audio, FLAC):
            meta['title'] = audio.get("title",[""])[0]
            meta['artist'] = audio.get("artist",[""])[0]
            meta['album'] = audio.get("album",[""])[0]
            meta['genre'] = audio.get("genre",[""])[0]
            meta['year'] = audio.get("date",[""])[0]
            meta['track_number'] = audio.get("tracknumber",[""])[0]
            meta['lyrics'] = audio.get("LYRICS",[""])[0] or audio.get("lyrics",[""])[0]
            if audio.pictures:
                meta['has_cover'] = True
                meta['cover_data'] = audio.pictures[0].data

        elif isinstance(audio, OggVorbis):
            meta['title'] = audio.get("title",[""])[0]
            meta['artist'] = audio.get("artist",[""])[0]
            meta['album'] = audio.get("album",[""])[0]
            meta['genre'] = audio.get("genre",[""])[0]
            meta['year'] = audio.get("date",[""])[0]
            meta['lyrics'] = audio.get("LYRICS",[""])[0] or audio.get("lyrics",[""])[0]

        elif isinstance(audio, MP4):
            meta['title'] = audio.get("\xa9nam",[""])[0]
            meta['artist'] = audio.get("\xa9ART",[""])[0]
            meta['album'] = audio.get("\xa9alb",[""])[0]
            meta['genre'] = audio.get("\xa9gen",[""])[0]
            meta['year'] = audio.get("\xa9day",[""])[0]
            meta['lyrics'] = audio.get("\xa9lyr",[""])[0] if "\xa9lyr" in audio else ""
            if "covr" in audio:
                meta['has_cover'] = True
                meta['cover_data'] = audio["covr"][0]

        for k in meta:
            if isinstance(meta[k], str): meta[k] = meta[k].strip()
    except Exception as e:
        print(f"  ⚠ Ошибка чтения {filepath}: {e}")
    return meta

def read_video_meta(filepath):
    """Базовые метаданные видео."""
    meta = {'title':'','duration':0}
    meta['title'] = os.path.splitext(os.path.basename(filepath))[0]
    if MUTAGEN:
        try:
            audio = mutagen.File(filepath)
            if audio and hasattr(audio.info, 'length'):
                meta['duration'] = int(audio.info.length)
        except Exception:
            swallow.note('media_indexer.read_video_meta:L171')
            pass
    return meta

# ===================== API ЗАПРОСЫ =====================

def lastfm_request(method, params):
    import urllib.parse
    p = {"method": method, "api_key": LASTFM_API_KEY, "format": "json", "lang": "ru"}
    p.update(params)
    return _http_get_json(f"{LASTFM_BASE}?{urllib.parse.urlencode(p)}")

def fetch_lyrics(artist, title):
    """Last.fm → Lyrics.ovh"""
    result = lastfm_request("track.getInfo", {"artist": artist, "track": title})
    if result and result.get("track",{}).get("wiki",{}).get("content"):
        wiki = result["track"]["wiki"]["content"]
        text = re.sub(r'<[^>]+>', '\n', wiki)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if len(text) > 50:
            return {"lyrics": text, "source": "last.fm", "synced": False, "lines": []}
    # Fallback
    try:
        import urllib.request, urllib.parse
        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}"
        data = _http_get_json(url, timeout=10)
        if data and data.get('lyrics'):
            return {"lyrics": data['lyrics'], "source": "lyrics.ovh", "synced": False, "lines": []}
    except Exception:
        swallow.note('media_indexer.fetch_lyrics:L200')
        pass
    return None

def fetch_cover(artist, album, title=None):
    """Обложка из Last.fm (трек → альбом)."""
    if title:
        result = lastfm_request("track.getInfo", {"artist": artist, "track": title})
        if result:
            images = result.get("track",{}).get("album",{}).get("image",[])
            for img in reversed(images):
                if img.get("#text"):
                    data = _http_get_bytes(img["#text"])
                    if data: return data
    if album and album != "Неизвестный альбом":
        result = lastfm_request("album.getInfo", {"artist": artist, "album": album})
        if result:
            images = result.get("album",{}).get("image",[])
            for img in reversed(images):
                if img.get("#text"):
                    data = _http_get_bytes(img["#text"])
                    if data: return data
    return None

def tmdb_search_movie(query, year=None):
    """Поиск фильма в TMDB."""
    import urllib.parse
    url = f"{TMDB_BASE}/search/movie?query={urllib.parse.quote(query)}&language=ru-RU"
    if year: url += f"&year={year}"
    req_headers = {"Authorization": f"Bearer {TMDB_API_KEY}"}
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        swallow.note('media_indexer.tmdb_search_movie:L236')
        return {"results": []}

def tmdb_get_movie(movie_id):
    """Детали фильма с актёрами."""
    import urllib.request
    url = f"{TMDB_BASE}/movie/{movie_id}?language=ru-RU&append_to_response=credits"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TMDB_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        swallow.note('media_indexer.tmdb_get_movie:L248')
        return None

def download_image(url, save_path):
    """Скачать изображение и сохранить."""
    if os.path.exists(save_path): return True
    data = _http_get_bytes(url)
    if data:
        with open(save_path, 'wb') as f: f.write(data)
        return True
    return False

# ===================== ГЛАВНАЯ ЛОГИКА =====================

def scan_directory(directory, extensions):
    """Рекурсивный скан директории."""
    results = []
    if not os.path.isdir(directory):
        print(f"  ⚠ {directory} не существует")
        return results
    for root, dirs, files in os.walk(directory):
        for fn in files:
            ext = os.path.splitext(fn.lower())[1]
            if ext in extensions:
                results.append(os.path.join(root, fn))
    return results

def index_music(directories, storage):
    """Индексация музыки."""
    print(f"\n🎵 Сканирование музыки...")
    all_files = []
    for d in directories:
        all_files.extend(scan_directory(d, AUDIO_EXTENSIONS))
    print(f"  Найдено файлов: {len(all_files)}")

    covers_dir = os.path.join(storage, "covers")
    lyrics_dir = os.path.join(storage, "lyrics")
    os.makedirs(covers_dir, exist_ok=True)
    os.makedirs(lyrics_dir, exist_ok=True)

    tracks = []
    for i, fp in enumerate(all_files):
        fn = os.path.basename(fp)
        meta = read_audio_meta(fp)
        track_id = hashlib.md5(fp.encode()).hexdigest()

        # Извлекаем путь из структуры папок
        rel = os.path.relpath(fp, directories[0])
        parts = Path(rel).parts
        if not meta['artist'] and len(parts) >= 2:
            meta['artist'] = parts[-2]
        if not meta['album'] and len(parts) >= 2:
            meta['album'] = parts[-2]

        track = {
            'id': track_id, 'filepath': fp, 'filename': fn,
            'title': meta['title'] or fn,
            'artist': meta['artist'] or 'Неизвестный исполнитель',
            'album': meta['album'] or 'Неизвестный альбом',
            'genre': meta['genre'], 'year': meta['year'],
            'track_number': meta['track_number'],
            'duration': meta['duration'], 'bitrate': meta['bitrate'],
            'sample_rate': meta['sample_rate'],
            'has_cover': meta['has_cover'], 'has_lyrics': bool(meta['lyrics']),
            'file_size': os.path.getsize(fp), 'file_ext': os.path.splitext(fn)[1].lower(),
        }

        # Сохраняем обложку из файла
        if meta.get('cover_data'):
            cover_path = os.path.join(covers_dir, f"{track_id}.jpg")
            with open(cover_path, 'wb') as f: f.write(meta['cover_data'])
            track['has_cover'] = True

        # Сохраняем текст из файла
        if meta.get('lyrics'):
            lpath = os.path.join(lyrics_dir, f"{track_id}.json")
            _save_json(lpath, {
                'track_id': track_id, 'lyrics': meta['lyrics'],
                'source': 'embedded', 'synced': False, 'lines': [],
                'track_title': track['title'], 'track_artist': track['artist']
            })
            track['has_lyrics'] = True

        tracks.append(track)

        if (i+1) % 10 == 0:
            print(f"  {i+1}/{len(all_files)}: {fn[:50]}")

    # Сохраняем индекс
    _save_json(os.path.join(storage, "music_index.json"), {
        'tracks': tracks, 'indexed_at': time.time(),
        'total_tracks': len(tracks), 'directories': directories
    })
    print(f"  ✅ Сохранено {len(tracks)} треков")

    # Загрузка текстов и обложек из интернета
    print(f"\n🌐 Загрузка текстов и обложек из Last.fm / Lyrics.ovh...")
    for i, track in enumerate(tracks):
        # Текст
        if not track['has_lyrics']:
            lyrics = fetch_lyrics(track['artist'], track['title'])
            if lyrics:
                lpath = os.path.join(lyrics_dir, f"{track['id']}.json")
                lyrics['track_id'] = track['id']
                lyrics['track_title'] = track['title']
                lyrics['track_artist'] = track['artist']
                _save_json(lpath, lyrics)
                track['has_lyrics'] = True

        # Обложка
        if not track['has_cover']:
            cover_data = fetch_cover(track['artist'], track['album'], track['title'])
            if cover_data:
                cpath = os.path.join(covers_dir, f"{track['id']}.jpg")
                with open(cpath, 'wb') as f: f.write(cover_data)
                track['has_cover'] = True

        if (i+1) % 5 == 0:
            lyr_cnt = sum(1 for t in tracks[:i+1] if t['has_lyrics'])
            cov_cnt = sum(1 for t in tracks[:i+1] if t['has_cover'])
            print(f"  {i+1}/{len(tracks)} | Тексты: {lyr_cnt} | Обложки: {cov_cnt}")

    # Пересохраняем
    _save_json(os.path.join(storage, "music_index.json"), {
        'tracks': tracks, 'indexed_at': time.time(),
        'total_tracks': len(tracks), 'directories': directories
    })

    lyr_total = sum(1 for t in tracks if t['has_lyrics'])
    cov_total = sum(1 for t in tracks if t['has_cover'])
    print(f"\n✅ Итого: {len(tracks)} треков, {lyr_total} с текстом, {cov_total} с обложкой")
    return tracks

def index_video(directories, storage):
    """Индексация видео с TMDB."""
    print(f"\n🎬 Сканирование видео...")
    all_files = []
    for d in directories:
        all_files.extend(scan_directory(d, VIDEO_EXTENSIONS))
    print(f"  Найдено файлов: {len(all_files)}")

    posters_dir = os.path.join(storage, "posters")
    backdrops_dir = os.path.join(storage, "backdrops")
    os.makedirs(posters_dir, exist_ok=True)
    os.makedirs(backdrops_dir, exist_ok=True)

    videos = []
    for i, fp in enumerate(all_files):
        fn = os.path.basename(fp)
        meta = read_video_meta(fp)

        # Очищаем название для поиска
        clean = re.sub(r'\.(mkv|mp4|avi|webm|mov|wmv|flv)', '', fn, flags=re.IGNORECASE).strip()
        clean = re.sub(r'[\(\[]\d{4}[\)\]]', '', clean).strip()
        clean = re.sub(r'\.\d{3,4}p\.', '', clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r'\.(BluRay|WEB-DL|WEBRip|HDRip|BDRip|DVDRip)', '', clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r'\.(x264|x265|h264|h265)', '', clean, flags=re.IGNORECASE).strip()
        clean = clean.replace('.', ' ').replace('_', ' ').strip()

        year_match = re.search(r'[\(\[](\d{4})[\)\]]', fn)
        year = year_match.group(1) if year_match else None

        video = {
            'id': hashlib.md5(fp.encode()).hexdigest(),
            'filepath': fp, 'filename': fn,
            'title': clean or fn, 'original_filename': fn,
            'duration': meta['duration'],
            'file_size': os.path.getsize(fp), 'file_ext': os.path.splitext(fn)[1].lower(),
        }

        # TMDB поиск
        print(f"  {i+1}/{len(all_files)}: {clean[:60]}...", end=' ')
        search = tmdb_search_movie(clean, year)
        if search and search.get('results'):
            movie = search['results'][0]
            tmdb_id = movie['id']
            details = tmdb_get_movie(tmdb_id)
            if details:
                video['tmdb_id'] = tmdb_id
                video['overview'] = details.get('overview','')
                video['year'] = details.get('release_date','')[:4]
                video['runtime'] = details.get('runtime',0)
                video['genres'] = [g['name'] for g in details.get('genres',[])]
                video['vote_average'] = details.get('vote_average',0)
                video['tagline'] = details.get('tagline','')
                dir_el = next((c for c in details.get('credits',{}).get('crew',[]) if c.get('job')=='Director'), None)
                if dir_el: video['director'] = dir_el['name']
                cast = details.get('credits',{}).get('cast',[])[:15]
                video['actors'] = [{'id':a['id'],'name':a['name'],'character':a['character'],
                                    'profile_path':a.get('profile_path','')} for a in cast if a.get('name')]

                # Скачиваем постер
                poster_path = details.get('poster_path')
                if poster_path:
                    poster_url = f"{TMDB_IMG}w500{poster_path}"
                    poster_local = os.path.join(posters_dir, f"{tmdb_id}_poster.jpg")
                    if download_image(poster_url, poster_local):
                        video['has_poster'] = True
                        video['poster_url'] = f"/api/media/poster/{tmdb_id}"

                # Скачиваем фон
                backdrop_path = details.get('backdrop_path')
                if backdrop_path:
                    backdrop_url = f"{TMDB_IMG}w1280{backdrop_path}"
                    backdrop_local = os.path.join(backdrops_dir, f"{tmdb_id}_backdrop.jpg")
                    if download_image(backdrop_url, backdrop_local):
                        video['backdrop_url'] = f"/api/media/backdrop/{tmdb_id}"
                print(f"✅ {video.get('title','?')} ({video.get('year','')})")
            else:
                print("❌ TMDB details error")
        else:
            print("❌ Не найдено")

        videos.append(video)

    _save_json(os.path.join(storage, "video_index.json"), {
        'videos': videos, 'indexed_at': time.time(),
        'total_videos': len(videos), 'directories': directories,
        'tmdb_loaded': sum(1 for v in videos if v.get('tmdb_id'))
    })

    tmdb_cnt = sum(1 for v in videos if v.get('tmdb_id'))
    print(f"\n✅ Итого: {len(videos)} видео, {tmdb_cnt} с метаданными TMDB")
    return videos

# ===================== CLI =====================

def main():
    parser = argparse.ArgumentParser(description='Индексация медиа с загрузкой метаданных')
    parser.add_argument('--music', nargs='+', help='Директории с музыкой')
    parser.add_argument('--video', nargs='+', help='Директории с видео')
    parser.add_argument('--storage', required=True, help='Директория для хранения данных')
    parser.add_argument('--music-only', action='store_true', help='Только музыка')
    parser.add_argument('--video-only', action='store_true', help='Только видео')
    args = parser.parse_args()

    storage = os.path.abspath(args.storage)
    os.makedirs(storage, exist_ok=True)
    print(f"📁 Хранилище: {storage}")

    if args.music and not args.video_only:
        index_music(args.music, storage)

    if args.video and not args.music_only:
        index_video(args.video, storage)

    print("\n🎉 Готово! Запустите server.py для использования.")

if __name__ == '__main__':
    main()
