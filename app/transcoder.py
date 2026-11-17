#!/usr/bin/env python3
"""
Infinity Transcoder Module - Отдельный модуль для транскодирования видео
"""

import os
import json
import hashlib
import re
import subprocess
import threading
import logging
from pathlib import Path
from typing import Optional, Callable, List, Tuple
import swallow

logger = logging.getLogger(__name__)

BROWSER_VIDEO_CODECS = {'h264', 'vp8', 'vp9', 'av1'}
BROWSER_AUDIO_CODECS = {'aac', 'mp3', 'opus', 'vorbis', 'flac'}

TRANSCODE_DIR = Path(__file__).parent.resolve() / "data" / "transcoded"
_HWACCEL_CACHE = None


def set_transcode_dir(path: Path):
    global TRANSCODE_DIR
    TRANSCODE_DIR = path
    TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# Скрытие консольных окон (Windows)
# ─────────────────────────────────────────────────────────────
# Сервер запускается без консоли, поэтому каждый дочерний ffmpeg без этого
# флага создавал бы своё окно и мигал чёрным во время открытия медиа.
# На POSIX Popen параметр creationflags не принимает — там пустой словарь.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _popen_kwargs():
    return {"creationflags": _NO_WINDOW} if os.name == "nt" else {}


def _run_kwargs():
    return {"creationflags": _NO_WINDOW} if os.name == "nt" else {}


def _probe_streams(filepath: str) -> list:
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', filepath]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **_run_kwargs())
        if r.returncode != 0:
            return []
        return json.loads(r.stdout).get('streams', [])
    except:
        swallow.note('transcoder._probe_streams:L56')
        return []


def _hwaccel_works(kind: str, device: str = '') -> bool:
    """Проверить, что конкретный аппаратный энкодер реально работает.

    ffmpeg -hwaccels перечисляет API, собранные в бинарнике, но не наличие
    GPU/драйвера: на машине без NVIDIA 'cuda' в списке есть, а h264_nvenc
    падает. Поэтому пробуем закодировать один кадр и смотрим код возврата.
    """
    src = ['-f', 'lavfi', '-i', 'color=black:s=64x64:d=0.1']
    if kind == 'nvenc':
        cmd = ['ffmpeg', '-v', 'error'] + src + ['-c:v', 'h264_nvenc', '-f', 'null', '-']
    elif kind == 'qsv':
        cmd = ['ffmpeg', '-v', 'error'] + src + ['-c:v', 'h264_qsv', '-f', 'null', '-']
    elif kind == 'videotoolbox':
        cmd = ['ffmpeg', '-v', 'error'] + src + ['-c:v', 'h264_videotoolbox', '-f', 'null', '-']
    elif kind == 'vaapi':
        if not device:
            return False
        cmd = (['ffmpeg', '-v', 'error', '-vaapi_device', device] + src +
               ['-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-'])
    else:
        return False
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25, **_run_kwargs())
        return r.returncode == 0
    except Exception:
        swallow.note('transcoder._hwaccel_works:L85')
        return False


def _detect_hwaccel() -> Tuple[str, str, list]:
    global _HWACCEL_CACHE
    if _HWACCEL_CACHE is not None:
        return _HWACCEL_CACHE

    hwaccel_type = 'software'
    hwaccel_device = ''
    hwaccel_filter_opts = []

    try:
        r = subprocess.run(['ffmpeg', '-hwaccels'], capture_output=True, text=True, timeout=10, **_run_kwargs())
        available = r.stdout.lower()

        vaapi_device = ''
        if 'vaapi' in available:
            for candidate in ['/dev/dri/renderD128', '/dev/dri/renderD129']:
                if os.path.exists(candidate):
                    vaapi_device = candidate
                    break

        candidates = []
        if 'cuda' in available or 'nvdec' in available:
            candidates.append('nvenc')
        if vaapi_device:
            candidates.append('vaapi')
        if 'qsv' in available:
            candidates.append('qsv')
        if 'videotoolbox' in available:
            candidates.append('videotoolbox')

        for kind in candidates:
            device = vaapi_device if kind == 'vaapi' else ''
            if _hwaccel_works(kind, device):
                hwaccel_type = kind
                if kind == 'vaapi':
                    hwaccel_device = device
                    hwaccel_filter_opts = ['-vf', 'format=nv12,hwupload']
                logger.info(f"HW acceleration detected: {hwaccel_type}")
                break
        if hwaccel_type == 'software' and candidates:
            logger.info("Аппаратные энкодеры недоступны — используется libx264")
    except Exception:
        swallow.note('transcoder._detect_hwaccel:L131')
        pass

    _HWACCEL_CACHE = (hwaccel_type, hwaccel_device, hwaccel_filter_opts)
    return _HWACCEL_CACHE


def needs_transcoding(filepath: str) -> bool:
    ext = Path(filepath).suffix.lower()
    streams = _probe_streams(filepath)
    if not streams:
        return ext not in ('.mp4', '.m4v', '.webm')
    video_codec = ''
    audio_codec = ''
    for s in streams:
        if s.get('codec_type') == 'video' and not video_codec:
            video_codec = s.get('codec_name', '').lower()
        elif s.get('codec_type') == 'audio' and not audio_codec:
            audio_codec = s.get('codec_name', '').lower()
    native_video = video_codec in BROWSER_VIDEO_CODECS
    native_audio = audio_codec in BROWSER_AUDIO_CODECS or not audio_codec
    if ext in ('.mp4', '.m4v'):
        return not (native_video and native_audio)
    elif ext == '.webm':
        return video_codec not in ('vp8', 'vp9', 'av1')
    else:
        return not (native_video and native_audio)


def needs_transcode_from_meta(video_codec: str, audio_codec: str, filepath: str) -> bool:
    ext = Path(filepath).suffix.lower()
    video_codec = (video_codec or '').lower()
    audio_codec = (audio_codec or '').lower()
    native_video = video_codec in BROWSER_VIDEO_CODECS
    native_audio = audio_codec in BROWSER_AUDIO_CODECS or not audio_codec
    if ext in ('.mp4', '.m4v'):
        return not (native_video and native_audio)
    elif ext == '.webm':
        return video_codec not in ('vp8', 'vp9', 'av1')
    else:
        return not (native_video and native_audio)


def needs_remux(filepath: str) -> bool:
    """Проверяет, достаточно ли ремукса (без перекодирования кодеков)"""
    ext = Path(filepath).suffix.lower()
    if ext in ('.mp4', '.m4v', '.webm'):
        return False
    streams = _probe_streams(filepath)
    if not streams:
        return False
    video_codec = ''
    audio_codec = ''
    for s in streams:
        if s.get('codec_type') == 'video' and not video_codec:
            video_codec = s.get('codec_name', '').lower()
        elif s.get('codec_type') == 'audio' and not audio_codec:
            audio_codec = s.get('codec_name', '').lower()
    native_video = video_codec in BROWSER_VIDEO_CODECS
    native_audio = audio_codec in BROWSER_AUDIO_CODECS or not audio_codec
    return native_video and native_audio


def _detect_audio_codec(filepath: str) -> str:
    for s in _probe_streams(filepath):
        if s.get('codec_type') == 'audio':
            return s.get('codec_name', 'unknown').lower()
    return 'unknown'


def _detect_video_codec(filepath: str) -> str:
    for s in _probe_streams(filepath):
        if s.get('codec_type') == 'video':
            return s.get('codec_name', 'unknown').lower()
    return 'unknown'


def _build_transcode_cmd(input_path, output_path, hwaccel_type='software', hwaccel_device='', hwaccel_filter_opts=None, remux=False):
    video_codec = _detect_video_codec(input_path)
    audio_codec = _detect_audio_codec(input_path)
    native_video = video_codec in BROWSER_VIDEO_CODECS
    native_audio = audio_codec in BROWSER_AUDIO_CODECS or not audio_codec
    cmd = ['ffmpeg', '-y']
    hwaccel_type, hwaccel_device, hwaccel_filter_opts = hwaccel_type or 'software', hwaccel_device or '', hwaccel_filter_opts or []

    if remux:
        cmd += ['-i', input_path]
        cmd += ['-c', 'copy']
        cmd += ['-movflags', '+faststart', '-f', 'mp4', output_path]
        return cmd

    if hwaccel_type == 'vaapi':
        cmd += ['-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi', '-vaapi_device', hwaccel_device]
    elif hwaccel_type == 'nvenc':
        cmd += ['-hwaccel', 'cuda']
    elif hwaccel_type in ('qsv', 'videotoolbox'):
        cmd += ['-hwaccel', hwaccel_type]
    cmd += ['-i', input_path]
    if hwaccel_type == 'vaapi' and hwaccel_filter_opts:
        cmd += hwaccel_filter_opts
    if native_video and native_audio:
        cmd += ['-c:v', 'copy', '-c:a', 'copy']
    else:
        if hwaccel_type == 'vaapi':
            cmd += ['-c:v', 'h264_vaapi']
        elif hwaccel_type == 'nvenc':
            cmd += ['-c:v', 'h264_nvenc', '-preset', 'p4']
        elif hwaccel_type == 'qsv':
            cmd += ['-c:v', 'h264_qsv']
        elif hwaccel_type == 'videotoolbox':
            cmd += ['-c:v', 'h264_videotoolbox']
        else:
            cmd += ['-c:v', 'libx264', '-preset', 'medium', '-crf', '23', '-pix_fmt', 'yuv420p', '-profile:v', 'high']
        if not native_audio:
            cmd += ['-c:a', 'aac', '-b:a', '128k']
        else:
            cmd += ['-c:a', 'copy']
    cmd += ['-movflags', '+faststart', '-f', 'mp4', output_path]
    return cmd


def transcode_video(input_path: str, output_path: str, progress_callback=None) -> bool:
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **_run_kwargs())
        duration = float(result.stdout.strip()) if result.stdout.strip() else 0

        cmd = [
            'ffmpeg', '-i', input_path,
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '23', '-pix_fmt', 'yuv420p', '-profile:v', 'high',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            '-y', output_path
        ]

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_popen_kwargs()
        )

        time_pattern = re.compile(r'time=(\d+):(\d+):(\d+\.?\d*)')

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            if progress_callback and line:
                match = time_pattern.search(line)
                if match and duration > 0:
                    h, m, s = match.groups()
                    current = int(h) * 3600 + int(m) * 60 + float(s)
                    progress = min(100, (current / duration) * 100)
                    progress_callback(progress, current, duration)

        return process.returncode == 0

    except Exception as e:
        logger.error(f"Ошибка транскодирования: {e}")
        return False


def _clean_transcache(max_size_mb: int = 5120):
    cache_dir = TRANSCODE_DIR
    if not cache_dir.exists():
        return
    files = []
    total = 0
    for f in cache_dir.iterdir():
        if f.is_file():
            sz = f.stat().st_size
            total += sz
            files.append((f.stat().st_mtime, f, sz))
    limit = max_size_mb * 1024 * 1024
    if total > limit:
        files.sort()
        for _, f, sz in files[:-20]:
            try:
                f.unlink()
                total -= sz
                if total <= limit:
                    break
            except:
                swallow.note('transcoder._clean_transcache:L318')
                pass


def do_remux(input_path, output_path):
    """Быстрый ремукс в MP4 без перекодирования"""
    cmd = _build_transcode_cmd(input_path, output_path, remux=True)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300, **_run_kwargs())
        if r.returncode == 0 and os.path.exists(output_path):
            logger.info(f"Remux успешен: {input_path} -> {output_path}")
            return True
        logger.warning(f"Remux не удался: {input_path}, код: {r.returncode}")
        return False
    except Exception as e:
        logger.warning(f"Remux ошибка: {e}")
        return False


def do_transcode(input_path, output_path, progress_callback=None):
    # Если можно просто ремукcнуть — делаем ремукс
    if needs_remux(input_path):
        return do_remux(input_path, output_path)

    hwaccel_type, hwaccel_device, hwaccel_filter_opts = _detect_hwaccel()
    cmd = _build_transcode_cmd(input_path, output_path, hwaccel_type, hwaccel_device, hwaccel_filter_opts)
    try:
        # stderr оставляем пайпом только когда его кто-то читает: иначе на
        # Windows буфер пайпа (~4 КБ) переполняется логом ffmpeg и процесс
        # навсегда блокируется в wait() — convert-avi «висел» намертво.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE if progress_callback else subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            **_popen_kwargs()
        )
        if progress_callback:
            duration = 0
            try:
                dur_cmd = ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                           '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
                dur_r = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=15, **_run_kwargs())
                duration = float(dur_r.stdout.strip()) if dur_r.stdout.strip() else 0
            except:
                swallow.note('transcoder.do_transcode:L366')
                pass
            time_pat = re.compile(r'time=(\d+):(\d+):(\d+\.?\d*)')
            while True:
                line = proc.stderr.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    m = time_pat.search(line)
                    if m and duration > 0:
                        h, m_, s = m.groups()
                        cur = int(h) * 3600 + int(m_) * 60 + float(s)
                        progress_callback(min(100, (cur / duration) * 100), cur, duration)
        else:
            proc.wait(timeout=7200)
        if proc.returncode == 0 and os.path.exists(output_path):
            _clean_transcache()
            return True
        return False
    except:
        swallow.note('transcoder.do_transcode:L386')
        return False


def _stream_transcode(filepath: str, chunk_size: int = 65536):
    # Проверяем кэш
    cache_path = get_cache_path(filepath)
    if os.path.exists(cache_path):
        logger.info(f"Stream transcode: используем кэш {cache_path}")
        with open(cache_path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        return

    # Если можно быстро ремукcнуть — делаем в файл и отдаём
    if needs_remux(filepath):
        if do_remux(filepath, cache_path):
            with open(cache_path, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            return

    # Транскодируем, сохраняя в кэш
    TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path + '.tmp'

    try:
        video_codec = _detect_video_codec(filepath)
        audio_codec = _detect_audio_codec(filepath)
        ext = Path(filepath).suffix.lower()
        native_video = video_codec in BROWSER_VIDEO_CODECS
        native_audio = audio_codec in BROWSER_AUDIO_CODECS or not audio_codec

        hwaccel_type, hwaccel_device, hwaccel_filter_opts = _detect_hwaccel()
        cmd = ['ffmpeg', '-y']
        if hwaccel_type == 'vaapi':
            cmd += ['-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi', '-vaapi_device', hwaccel_device]
        elif hwaccel_type == 'nvenc':
            cmd += ['-hwaccel', 'cuda']
        elif hwaccel_type in ('qsv', 'videotoolbox'):
            cmd += ['-hwaccel', hwaccel_type]
        cmd += ['-i', filepath]
        if hwaccel_type == 'vaapi' and hwaccel_filter_opts:
            cmd += hwaccel_filter_opts

        if native_video:
            cmd += ['-c:v', 'copy']
        elif hwaccel_type == 'vaapi':
            cmd += ['-c:v', 'h264_vaapi']
        elif hwaccel_type == 'nvenc':
            cmd += ['-c:v', 'h264_nvenc', '-preset', 'p4']
        elif hwaccel_type == 'qsv':
            cmd += ['-c:v', 'h264_qsv']
        elif hwaccel_type == 'videotoolbox':
            cmd += ['-c:v', 'h264_videotoolbox']
        else:
            cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23', '-pix_fmt', 'yuv420p', '-profile:v', 'high']
        if native_audio and native_video:
            cmd += ['-c:a', 'copy']
        else:
            cmd += ['-c:a', 'aac', '-b:a', '192k']
        cmd += ['-f', 'mp4']
        cmd += ['-movflags', 'frag_keyframe+empty_moov']
        cmd += ['-threads', '0']
        cmd += ['-y', tmp_path]

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_popen_kwargs()
        )

        process.wait(timeout=7200)

        if process.returncode == 0 and os.path.exists(tmp_path):
            os.replace(tmp_path, cache_path)
            _clean_transcache()
            with open(cache_path, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            return

        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

        raise Exception(f"ffmpeg return code {process.returncode}")

    except Exception as e:
        logger.error(f"Stream transcode error for {filepath}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except:
            swallow.note('transcoder._stream_transcode:L490')
            pass
        try:
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e2:
            logger.error(f"Fallback read error: {e2}")


def stream_transcode_live(filepath: str, chunk_size: int = 65536):
    """Настоящее потоковое транскодирование mp4 напрямую в stdout (fragmented mp4).
    Видео начинает воспроизводиться почти сразу; процесс ffmpeg убивается при отвале клиента.
    Если в кэше уже есть готовый файл — отдаём его.
    """
    cache_path = get_cache_path(filepath)
    if os.path.exists(cache_path):
        logger.info(f"Stream transcode: используем кэш {cache_path}")
        with open(cache_path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        return

    hwaccel_type, hwaccel_device, hwaccel_filter_opts = _detect_hwaccel()
    cmd = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-y']
    if hwaccel_type == 'vaapi':
        cmd += ['-hwaccel', 'vaapi', '-hwaccel_output_format', 'vaapi', '-vaapi_device', hwaccel_device]
    elif hwaccel_type == 'nvenc':
        cmd += ['-hwaccel', 'cuda']
    elif hwaccel_type in ('qsv', 'videotoolbox'):
        cmd += ['-hwaccel', hwaccel_type]
    cmd += ['-i', filepath]
    if hwaccel_type == 'vaapi' and hwaccel_filter_opts:
        cmd += hwaccel_filter_opts

    # Принудительно перекодируем в браузерные кодеки
    if hwaccel_type == 'vaapi':
        cmd += ['-c:v', 'h264_vaapi']
    elif hwaccel_type == 'nvenc':
        cmd += ['-c:v', 'h264_nvenc', '-preset', 'p4']
    elif hwaccel_type == 'qsv':
        cmd += ['-c:v', 'h264_qsv']
    elif hwaccel_type == 'videotoolbox':
        cmd += ['-c:v', 'h264_videotoolbox']
    else:
        cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23', '-pix_fmt', 'yuv420p', '-profile:v', 'high']
    cmd += ['-c:a', 'aac', '-b:a', '192k']
    cmd += ['-movflags', 'frag_keyframe+empty_moov']
    cmd += ['-threads', '0']
    cmd += ['-f', 'mp4', 'pipe:1']

    hw_variant = hwaccel_type in ('nvenc', 'qsv', 'videotoolbox', 'vaapi')
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **_popen_kwargs())

    def _pump_stderr(p):
        try:
            data = p.stderr.read()
            tail = data.decode('utf-8', errors='ignore')[-1200:]
            if tail.strip():
                logger.warning(f"On-the-fly transcode stderr: {tail}")
        except Exception:
            swallow.note('transcoder._pump_stderr:L557')
            pass

    threading.Thread(target=_pump_stderr, args=(proc,), daemon=True).start()

    def _stream_proc(p):
        sent = 0
        try:
            while True:
                chunk = p.stdout.read(chunk_size)
                if not chunk:
                    break
                sent += len(chunk)
                yield chunk
        finally:
            if p.poll() is None:
                p.kill()
                try:
                    p.wait(timeout=10)
                except Exception:
                    swallow.note('transcoder._stream_proc:L577')
                    pass
        return sent

    try:
        gen = _stream_proc(proc)
        sent = 0
        for chunk in gen:
            sent += 1
            yield chunk
        try:
            proc.wait(timeout=5)
        except Exception:
            swallow.note('transcoder.stream_transcode_live:L590')
            pass
        rc = proc.returncode
    except GeneratorExit:
        raise
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise

    if sent == 0 and rc not in (0, None) and hw_variant:
        # Аппаратный вариант упал (нет GPU/CUDA) — повтор в чистом софте
        logger.info(f"On-the-fly transcode: hardware encode failed (rc={rc}), "
                    f"повторяем через libx264 без аппаратного ускорения")
        sw_cmd = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-hwaccel', 'none',
                  '-i', filepath, '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                  '-pix_fmt', 'yuv420p', '-profile:v', 'high',
                  '-c:a', 'aac', '-b:a', '192k',
                  '-movflags', 'frag_keyframe+empty_moov', '-threads', '0',
                  '-f', 'mp4', 'pipe:1']
        proc2 = subprocess.Popen(sw_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **_popen_kwargs())
        threading.Thread(target=_pump_stderr, args=(proc2,), daemon=True).start()
        try:
            gen2 = _stream_proc(proc2)
            sent = 0
            for chunk in gen2:
                sent += 1
                yield chunk
        finally:
            if proc2.poll() is None:
                proc2.kill()
                try:
                    proc2.wait(timeout=10)
                except Exception:
                    swallow.note('transcoder.stream_transcode_live:L624')
                    pass
        if sent == 0:
            raise RuntimeError("ffmpeg не смог транскодировать файл на лету")
        return
    if sent == 0 and rc not in (0, None):
        raise RuntimeError(f"ffmpeg упал при открытии файла (rc={rc})")
    logger.info(f"On-the-fly transcode finished: {os.path.basename(filepath)}")


def get_cache_path(filepath: str) -> str:
    file_id = hashlib.md5(filepath.encode('utf-8')).hexdigest()
    return str(TRANSCODE_DIR / f"{file_id}.mp4")
