#!/usr/bin/env python3
"""
Infinity VLC Streamer — транскодирование видео через VLC в HLS поток.

VLC пишет HLS (stream.m3u8 + .ts-сегменты) в каталог сессии,
а файлы раздаёт HTTP-сервер приложения через /api/media/vlc-hls/<session>/<file>.
AppImage автоматически распаковывается один раз (не требует FUSE/root).
"""

import os
import sys
import time
import uuid
import json
import atexit
import re
import shutil
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional
import swallow

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Скрытие консольных окон (Windows)
# ─────────────────────────────────────────────────────────────
# При входе на страницу Media вызывается vlc_available() -> find_vlc(),
# который зондирует VLC через `where vlc` и `vlc --version`. Сервер запущен
# без консоли, поэтому каждый такой вызов открывал всплывающее чёрное окно.
# CREATE_NO_WINDOW даёт потомку скрытую консоль. На POSIX параметр не
# принимается, поэтому там подставляем пустые kwargs.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run_kwargs():
    return {"creationflags": _NO_WINDOW} if os.name == "nt" else {}

BUNDLE_DIR = Path(__file__).parent.resolve() / "vlc" / "portable"
APPIMAGE_DIRS = [
    BUNDLE_DIR,
    Path(__file__).parent.resolve() / "vls",  # на случай опечатки
    Path(__file__).parent.resolve() / "vlc",
]
VLC_PROCESSES: dict = {}
VLC_LOCK = threading.Lock()
_VLC_PREP_LOCK = threading.Lock()


def _win_bundle_dirs():
    """Каталоги portable-VLC для Windows.

    В комплекте лежит распакованный vlc-3.0.21/vlc.exe, но структура может
    отличаться (vlc.exe в корне portable/ или в подпапке версии) — поэтому
    проверяем оба варианта и любую вложенную папку с vlc.exe.
    """
    out = []
    if BUNDLE_DIR.is_dir():
        direct = BUNDLE_DIR / "vlc.exe"
        if direct.is_file():
            out.append(direct)
        try:
            for child in sorted(BUNDLE_DIR.iterdir()):
                cand = child / "vlc.exe"
                if child.is_dir() and cand.is_file():
                    out.append(cand)
        except OSError:
            swallow.note('vlc_streamer._win_bundle_dirs:L69')
            pass
    return out

# ─── WSL / PLATFORM DETECTION ────────────────────────────────────────────────
_IS_WSL = False
_IS_WINDOWS = sys.platform == 'win32'
if not _IS_WINDOWS:
    try:
        with open('/proc/version', 'r') as _f:
            _IS_WSL = 'microsoft' in _f.read().lower()
    except Exception:
        swallow.note('vlc_streamer.<module>:L81')
        pass

# ─── PATH HELPERS ────────────────────────────────────────────────────────────

def _to_windows_path(p: str) -> str:
    """Приводит путь к Windows-формату для запуска VLC (Windows exe) из WSL"""
    if not p:
        return p
    p = p.strip().replace('\\', '/')
    # /mnt/c/Users/file → C:\Users\file
    sep = '\\'
    m = re.match(r'^/(?:mnt|mnt)/([a-zA-Z])/(.*)', p)
    if m:
        return f"{m.group(1).upper()}:{sep}{m.group(2).replace('/', sep)}"
    # Уже Windows-формат: C:/path → C:\path
    m = re.match(r'^([a-zA-Z]):[/\\](.*)', p)
    if m:
        return f"{m.group(1).upper()}:{sep}{m.group(2).replace('/', sep)}"
    return p


def _is_windows_exe(path: str) -> bool:
    """Определяет, является ли VLC бинарник Windows-исполняемым (.exe)"""
    return path.lower().endswith('.exe')

# ─── РАСПОЗНАВАНИЕ VLC ───────────────────────────────────────────────────────

def _appimage_extract_dir() -> Path:
    """Каталог, куда один раз распаковывается AppImage (без root/FUSE)."""
    return Path.home() / ".infinity" / "data" / "vlc_appimage"


def _extracted_vlc() -> Optional[str]:
    """Путь к уже извлечённому из AppImage бинарю vlc (если есть)."""
    exe = _appimage_extract_dir() / "usr" / "bin" / "vlc"
    return str(exe) if exe.exists() else None


def _find_appimage() -> Optional[str]:
    for d in APPIMAGE_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*[Vv][Ll][Cc]*.AppImage"), key=lambda p: str(p.stem), reverse=True):
            return str(f)
    return None


_WHICH_CACHE = {"path": None, "ts": 0.0}
_WHICH_TTL = 10.0
_WHICH_LOCK = threading.Lock()


def _which_vlc() -> Optional[str]:
    """Системный VLC из PATH.

    Прежний `where vlc` — подпроцесс на 176 мс, и это была единственная статья
    холодного vlc_check, вылезающая за 200 мс. shutil.which делает ту же работу
    внутри процесса (~30 мс). Ответ кэшируется на 10 с: установленный VLC за
    это время никуда не денется, а опрос идёт при каждом открытии Media.
    """
    now = time.time()
    with _WHICH_LOCK:
        if now - _WHICH_CACHE["ts"] < _WHICH_TTL:
            return _WHICH_CACHE["path"]
    try:
        import shutil
        path = shutil.which("vlc")
    except Exception:
        swallow.note('vlc_streamer._which_vlc:L150')
        path = None
    with _WHICH_LOCK:
        _WHICH_CACHE["path"] = path
        _WHICH_CACHE["ts"] = time.time()
    return path


def find_vlc() -> Optional[str]:
    candidates = []

    # 1. Уже извлечённый AppImage (самый надёжный в WSL без FUSE)
    extracted = _extracted_vlc()
    if extracted:
        candidates.append(extracted)

    # 2. Системный VLC (Linux native, apt-установленный, или PATH)
    path = _which_vlc()
    if path:
        candidates.append(path)

    # 3. Бандл VLC в проекте (AppImage для Linux, exe для Windows)
    if not extracted and not _IS_WINDOWS:
        appimage = _find_appimage()
        if appimage:
            candidates.append(appimage)
    # 3a. Portable VLC из комплекта (Windows) — приоритетнее системного,
    #     чтобы стриминг работал на машине без установленного VLC.
    for exe in _win_bundle_dirs():
        candidates.append(str(exe))
    if sys.platform == 'win32' or _IS_WSL:
        bundle_exe = BUNDLE_DIR / "vlc.exe"
        if bundle_exe.exists():
            candidates.append(str(bundle_exe))

    # 4. Типовые пути установки
    if _IS_WSL:
        for letter in ['c', 'd']:
            for pf in [f"/mnt/{letter}/Program Files", f"/mnt/{letter}/Program Files (x86)"]:
                vlc_path = os.path.join(pf, "VideoLAN", "VLC", "vlc.exe")
                if os.path.exists(vlc_path):
                    candidates.append(vlc_path)
    elif sys.platform == 'win32':
        for pf in [os.environ.get('ProgramFiles', 'C:\\Program Files'),
                   os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)')]:
            vlc_path = os.path.join(pf, "VideoLAN", "VLC", "vlc.exe")
            if os.path.exists(vlc_path):
                candidates.append(vlc_path)

    # Убираем дубликаты с сохранением порядка
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    return unique[0] if unique else None


def prepare_vlc() -> Optional[str]:
    """Возвращает запускаемый VLC-бинарник.
    Если найден только AppImage — один раз распаковывает его в ~/.infinity/data/vlc_appimage
    (не требует FUSE и прав root). Возвращает None, если VLC недоступен.
    """
    vlc_path = find_vlc()
    if not vlc_path:
        return None

    # Не AppImage — обычный бинарник
    if not vlc_path.lower().endswith('.appimage'):
        if _IS_WINDOWS or _IS_WSL:
            if _is_windows_exe(vlc_path):
                return vlc_path
        if not _IS_WINDOWS and not os.access(vlc_path, os.X_OK):
            try:
                os.chmod(vlc_path, os.stat(vlc_path).st_mode | 0o111)
            except Exception:
                swallow.note('vlc_streamer.prepare_vlc:L228')
                pass
        return vlc_path

    # AppImage → однократная распаковка
    with _VLC_PREP_LOCK:
        extracted = _extracted_vlc()
        if extracted:
            return extracted

        src = Path(vlc_path)
        if not src.exists():
            return None
        try:
            os.chmod(str(src), os.stat(str(src)).st_mode | 0o111)
        except Exception:
            swallow.note('vlc_streamer.prepare_vlc:L244')
            pass

        out_dir = _appimage_extract_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"VLC: однократное извлечение AppImage -> {out_dir}")
        try:
            r = subprocess.run([str(src), '--appimage-extract'], cwd=str(out_dir),
                               capture_output=True, timeout=900, **_run_kwargs())
        except Exception as e:
            logger.warning(f"VLC: ошибка запуска AppImage extraction: {e}")
            return None

        if r.returncode != 0:
            msg = r.stderr.decode('utf-8', errors='ignore')[-400:]
            logger.warning(f"VLC: AppImage extraction failed: {msg}")
            return None

        squash = out_dir / 'squashfs-root'
        exe = squash / 'usr' / 'bin' / 'vlc'
        if not exe.exists():
            logger.warning(f"VLC: извлечённый бинарь не найден: {exe}")
            shutil.rmtree(str(squash), ignore_errors=True)
            return None

        # Переносим содержимое squashfs-root на верхний уровень каталога
        for item in list(squash.iterdir()):
            target = out_dir / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(str(target), ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            shutil.move(str(item), str(target))
        shutil.rmtree(str(squash), ignore_errors=True)

        exe = out_dir / 'usr' / 'bin' / 'vlc'
        if not exe.exists():
            logger.warning(f"VLC: извлечённый бинарь не найден: {exe}")
            return None

        try:
            os.chmod(str(exe), os.stat(str(exe)).st_mode | 0o111)
        except Exception:
            swallow.note('vlc_streamer.prepare_vlc:L289')
            pass

        logger.info(f"VLC: AppImage извлечён: {exe}")
        return str(exe)


# ─── HLS СЕССИИ ──────────────────────────────────────────────────────────────

_HLS_ROOT: Optional[Path] = None


def set_hls_root(path):
    """Внешний каталог для HLS-сессий (устанавливается приложением)."""
    global _HLS_ROOT
    _HLS_ROOT = Path(path)
    try:
        _HLS_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        swallow.note('vlc_streamer.set_hls_root:L308')
        pass


def _hls_root() -> Path:
    if _HLS_ROOT and _HLS_ROOT.exists():
        return _HLS_ROOT
    root = Path.home() / ".infinity" / "data" / "vlc_hls"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        swallow.note('vlc_streamer._hls_root:L319')
        pass
    return root


_VERSION_CACHE = {}


def _version_from_path(vlc_path: str) -> str:
    """Версия из имени каталога/файла, без запуска процесса.

    Portable-сборка лежит как ...\\vlc-3.0.21\\vlc.exe, поэтому номер
    версии уже есть в пути. Берём его оттуда — это мгновенно и не требует
    запуска vlc.exe, который на Windows всё равно не печатает версию.
    """
    for part in reversed(Path(vlc_path).parts[:-1]):
        m = re.search(r'(\d+\.\d+(?:\.\d+)?)', part)
        if m:
            return f"VLC {m.group(1)}"
    return ''


def _vlc_version(vlc_path: str) -> str:
    """Версия VLC. Результат кэшируется навсегда для данного бинаря.

    Сначала пробуем вытащить версию из пути — это бесплатно. Только если
    не вышло, запускаем `vlc --version`: portable vlc.exe на Windows —
    GUI-приложение, на этом ключе он не завершается, и мы ждали полный
    timeout (10 с) впустую. Из-за этого вход на страницу Media
    подвисал на десять секунд при каждом открытии.
    """
    if vlc_path in _VERSION_CACHE:
        return _VERSION_CACHE[vlc_path]

    version = _version_from_path(vlc_path)
    if not version:
        try:
            r = subprocess.run([vlc_path, '--version'], capture_output=True,
                               text=True, timeout=10, **_run_kwargs())
            if r.returncode == 0:
                version = r.stdout.strip().split('\n')[0]
        except Exception:
            swallow.note('vlc_streamer._vlc_version:L361')
            version = ''

    _VERSION_CACHE[vlc_path] = version
    return version


def _vlc_hls_supported(vlc_path: str) -> bool:
    """Есть ли в этой сборке VLC hls-муксер?
    Учитывает раскладку плагинов:
    - Windows exe: <каталог VLC>/plugins/mux/hls/libhls_plugin.dll
    - Linux обычный: <каталог бинаря>/../lib/vlc/plugins/mux/hls/libhls_plugin.so
    - Linux AppImage/juNest: ../../usr/lib/vlc/plugins/mux/hls/libhls_plugin.so
    В большинстве Linux-сборок (Ubuntu/Debian/AppImage) hls-муксер отсутствует —
    тогда движок честно откатывается на FFmpeg.
    """
    try:
        exe_dir = os.path.dirname(vlc_path)
        if _is_windows_exe(vlc_path):
            candidates = [
                os.path.join(exe_dir, 'plugins', 'mux', 'hls', 'libhls_plugin.dll'),
                os.path.join(exe_dir, 'plugins', 'mux', 'hls', 'libhls_plugin.so'),
            ]
        else:
            candidates = [
                os.path.join(exe_dir, '..', 'lib', 'vlc', 'plugins', 'mux', 'hls', 'libhls_plugin.so'),
                os.path.join(exe_dir, 'lib', 'vlc', 'plugins', 'mux', 'hls', 'libhls_plugin.so'),
                os.path.join(exe_dir, '..', '..', 'usr', 'lib', 'vlc', 'plugins', 'mux', 'hls', 'libhls_plugin.so'),
            ]
        return any(os.path.isfile(p) for p in candidates)
    except Exception:
        swallow.note('vlc_streamer._vlc_hls_supported:L392')
        return False


def bundled_ffmpeg_dir():
    """Каталог с портативным ffmpeg из комплекта (app/binaries/ffmpeg/.../bin)."""
    base = Path(__file__).parent.resolve() / "binaries" / "ffmpeg"
    if not base.is_dir():
        return None
    # 1) уже разложенный bin
    direct = base / "bin"
    if (direct / "ffmpeg.exe").is_file() or (direct / "ffmpeg").is_file():
        return direct
    # 2) распакованная сборка ffmpeg-X.Y.Z-.../bin
    try:
        for child in sorted(base.iterdir()):
            cand = child / "bin"
            if child.is_dir() and ((cand / "ffmpeg.exe").is_file()
                                   or (cand / "ffmpeg").is_file()):
                return cand
    except OSError:
        swallow.note('vlc_streamer.bundled_ffmpeg_dir:L413')
        pass
    return None


def ffmpeg_path() -> Optional[str]:
    """Полный путь к ffmpeg: сначала портативный из комплекта, затем PATH."""
    bundled = bundled_ffmpeg_dir()
    if bundled:
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = bundled / name
            if p.is_file():
                return str(p)
    return shutil.which("ffmpeg")


def _ffmpeg_exists() -> bool:
    try:
        return ffmpeg_path() is not None
    except Exception:
        swallow.note('vlc_streamer._ffmpeg_exists:L433')
        return False


def start_stream(filepath: str) -> dict:
    """Запускает транскодирование в HLS и отдаёт URL сессии.

    Движок выбирается автоматически:
    - VLC (если в сборке есть hls-муксер),
    - иначе FFmpeg (hls-муксер есть в любом ffmpeg).
    VLC/FFmpeg пишут stream.m3u8 + .ts-сегменты в каталог сессии,
    файлы раздаёт HTTP-сервер приложения (route /api/media/vlc-hls/<session>/<file>).
    """
    vlc_path = prepare_vlc()
    if vlc_path and _vlc_hls_supported(vlc_path):
        engine = 'vlc'
    elif _ffmpeg_exists():
        engine = 'ffmpeg'
    elif vlc_path:
        raise RuntimeError(
            "В этой сборке VLC нет поддержки HLS-муксера, а FFmpeg не найден. "
            "Установите ffmpeg или используйте другой способ проигрывания."
        )
    else:
        raise RuntimeError(
            "VLC не найден или не может запуститься. "
            "Установите VLC либо переключите движок на FFmpeg в настройках медиа."
        )

    # Конвертируем путь в зависимости от типа VLC (Windows exe / Linux)
    if engine == 'vlc':
        if _is_windows_exe(vlc_path) and _IS_WSL:
            actual_path = _to_windows_path(filepath)
        else:
            actual_path = filepath
    else:
        actual_path = filepath

    if not os.path.exists(actual_path):
        raise RuntimeError(f"Файл не найден: {actual_path}")

    with VLC_LOCK:
        session_id = uuid.uuid4().hex[:12]
        session_dir = _hls_root() / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        log_path = session_dir / "vlc.log"
        try:
            log_handle = open(str(log_path), 'ab', buffering=0)
        except Exception:
            swallow.note('vlc_streamer.start_stream:L483')
            log_handle = subprocess.DEVNULL

        if engine == 'vlc':
            sout = (
                f"#transcode{{vcodec=h264,acodec=mpga,scale=auto,venc=x264{{profile=high,level=4.0,crf=23}},"
                f"afilter=aresample={{sample_rate=44100,channels=2}}}}:"
                f"std{{access=file,mux=hls,dst={session_dir}/stream.m3u8}}"
            )
            cmd = [
                vlc_path, '-I', 'dummy',
                actual_path,
                '--sout', sout,
                '--sout-keep',
                '--play-and-exit',
                '--network-caching=100',
                '--no-stats',
                '--no-video-title-show',
                '--no-osd',
                '-vvv',
            ]
        else:
            cmd = [
                'ffmpeg', '-nostdin', '-loglevel', 'warning', '-y',
                '-i', actual_path,
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-pix_fmt', 'yuv420p', '-bf', '0',
                '-x264-params', 'keyint=100:min-keyint=100:scenecut=0',
                '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2',
                '-f', 'hls',
                '-hls_time', '4',
                '-hls_list_size', '0',
                '-hls_segment_filename', str(session_dir / 'seg%d.ts'),
                '-threads', '0',
                str(session_dir / 'stream.m3u8'),
            ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
        except Exception as e:
            if log_handle is not subprocess.DEVNULL:
                try:
                    log_handle.close()
                except Exception:
                    swallow.note('vlc_streamer.start_stream:L532')
                    pass
            shutil.rmtree(str(session_dir), ignore_errors=True)
            raise RuntimeError(f"Ошибка запуска {engine}: {e}")

        stream_url = f"/api/media/vlc-hls/{session_id}/stream.m3u8"
        VLC_PROCESSES[session_id] = {
            'process': proc,
            'engine': engine,
            'filepath': filepath,
            'stream_url': stream_url,
            'dir': str(session_dir),
            'log': log_handle,
            'started_at': time.time(),
        }

        logger.info(f"HLS stream started ({engine}): {session_id} file={os.path.basename(filepath)}")

        return {
            'session_id': session_id,
            'stream_url': stream_url,
            'engine': engine,
            'port': None,
        }


def stop_stream(session_id: str) -> bool:
    with VLC_LOCK:
        info = VLC_PROCESSES.pop(session_id, None)
    if not info:
        return False

    proc = info['process']
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                swallow.note('vlc_streamer.stop_stream:L571')
                pass
    except Exception:
        swallow.note('vlc_streamer.stop_stream:L574')
        try:
            proc.kill()
        except Exception:
            swallow.note('vlc_streamer.stop_stream:L578')
            pass

    log_handle = info.get('log')
    if log_handle is not None and log_handle is not subprocess.DEVNULL:
        try:
            log_handle.close()
        except Exception:
            swallow.note('vlc_streamer.stop_stream:L586')
            pass

    session_dir = info.get('dir')
    if session_dir:
        shutil.rmtree(session_dir, ignore_errors=True)

    logger.info(f"VLC stream stopped: {session_id}")
    return True


def get_stream_info(session_id: str) -> Optional[dict]:
    info = VLC_PROCESSES.get(session_id)
    if not info:
        return None
    proc = info['process']
    seg_count = 0
    last_seg_mtime = 0.0
    eos = False
    sdir = info.get('dir')
    if sdir:
        try:
            ts_files = [os.path.join(sdir, n) for n in os.listdir(sdir) if n.endswith('.ts')]
            seg_count = len(ts_files)
            if ts_files:
                last_seg_mtime = max(os.path.getmtime(p) for p in ts_files)
            m3u = os.path.join(sdir, 'stream.m3u8')
            if os.path.isfile(m3u):
                with open(m3u, 'r', errors='ignore') as f:
                    eos = '#EXT-X-ENDLIST' in f.read()
        except Exception:
            swallow.note('vlc_streamer.get_stream_info:L617')
            pass
    return {
        'session_id': session_id,
        'stream_url': info['stream_url'],
        'filepath': info['filepath'],
        'dir': sdir,
        'engine': info.get('engine', 'vlc'),
        'alive': proc.poll() is None,
        'uptime': time.time() - info['started_at'],
        'segments': seg_count,
        'last_seg_mtime': last_seg_mtime,
        'eos': eos,
    }


def cleanup_all():
    with VLC_LOCK:
        session_ids = list(VLC_PROCESSES.keys())
    for sid in session_ids:
        stop_stream(sid)
    logger.info("All VLC streams cleaned up")


_CHECK_CACHE = {"data": None, "ts": 0.0}
_CHECK_TTL = 60.0


def vlc_check() -> dict:
    """Статус движков: VLC и FFmpeg (HLS-фолбэк).

    Результат кэшируется на CHECK_TTL секунд: страница Media дёргает эту
    проверку при каждом открытии, а поиск VLC в PATH (`where vlc`) стоит
    запуска внешнего процесса. Установленный VLC за минуту не появляется.
    """
    now = time.time()
    if _CHECK_CACHE["data"] is not None and now - _CHECK_CACHE["ts"] < _CHECK_TTL:
        return _CHECK_CACHE["data"]

    vlc_path = find_vlc()
    ffmpeg_ok = _ffmpeg_exists()
    hls_engine = 'ffmpeg'
    hls_muxer = False
    note = ''
    if vlc_path:
        # AppImage ещё не распакован — запускаем распаковку в фоне, чтобы не блокировать старт
        if vlc_path.lower().endswith('.appimage') and not _extracted_vlc():
            threading.Thread(target=lambda: prepare_vlc(), daemon=True).start()
        hls_muxer = _vlc_hls_supported(vlc_path)
        if hls_muxer:
            hls_engine = 'vlc'
        elif ffmpeg_ok:
            note = ('VLC найден, но в этой сборке нет HLS-муксера '
                    '(Linux-сборки обычно его не содержат) — поток будет создавать FFmpeg.')
        else:
            note = 'VLC найден, но нет HLS-муксера, а FFmpeg недоступен — воспроизведение невозможно.'
    elif not ffmpeg_ok:
        note = 'Ни VLC, ни FFmpeg не найдены — настройте один из движков.'
    result = {
        'available': bool(vlc_path),
        'path': vlc_path,
        'version': _vlc_version(vlc_path) if vlc_path else None,
        'ffmpeg_available': ffmpeg_ok,
        'hls_muxer': hls_muxer,
        'hls_engine': hls_engine,
        'note': note,
    }
    _CHECK_CACHE["data"] = result
    _CHECK_CACHE["ts"] = time.time()
    return result


atexit.register(cleanup_all)