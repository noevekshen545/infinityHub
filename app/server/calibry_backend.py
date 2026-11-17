#!/usr/bin/env python3
"""Calibre-бэкенд для сервиса книг.

Сервер может работать в WSL (Linux) или Windows — тогда используется портабельный
Calibre (папка calibry/Calibre Portable). На чистом Linux (сборка AppImage)
используется AppImage Calibre (calibry/*.AppImage), который автоматически
распаковывается, а его CLI-инструменты (ebook-convert, ebook-meta, ebook-polish,
fetch-ebook-metadata) запускаются через subprocess с таймаутами и блокировкой.

Ничего не импортирует из server_backup на уровне модуля (избегаем циклов),
зависимые функции импортируются лениво внутри методов.
"""

import os
import re
import sys
import io
import json
import time
import glob
import hashlib
import shutil
import subprocess
import threading
import swallow

_lock = threading.Lock()

# ─────────────────────────── платформа и пути ───────────────────────────

def is_wsl() -> bool:
    """True, если текущий Python работает внутри WSL."""
    if sys.platform != 'linux':
        return False
    return os.path.exists('/proc/sys/fs/binfmt_misc/WSLInterop') or '/mnt/c' in os.listdir('/')


def _bundle_root() -> str:
    """Корень переносимой сборки (папка, где лежат app/, calibry/, data/).

    Раньше здесь возвращался родитель папки server/ — то есть app/. Но в
    переносимой сборке Calibre лежит в calibry/ рядом с app/, поэтому
    бэкенд искал его в app/calibry и не находил ничего.
    """
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # app/calibry (старая раскладка) — вернуться на уровень выше
    if os.path.isdir(os.path.join(os.path.dirname(app_dir), "calibry")):
        return os.path.dirname(app_dir)
    return app_dir


def _runjson_root() -> str:
    """Корень проекта runjson (родитель папки server/)."""
    return _bundle_root()


def _cfg():
    try:
        from . import config
        cfg = config.CFG
    except Exception:
        swallow.note('calibry_backend._cfg:L61')
        try:
            import server_backup as _sb
            cfg = getattr(_sb, 'CFG', {})
        except Exception:
            swallow.note('calibry_backend._cfg:L66')
            cfg = {}
    return cfg or {}


def get_calibre_dir() -> str:
    """Директория с exe-файлами Calibre (на стороне текущей ОС)."""
    over = os.environ.get('CALIBRY_DIR') or (_cfg().get('calibry_dir') or '')
    if over:
        p = os.path.expanduser(over)
        if is_wsl():
            p = _win_to_posix(p)
        return p
    cands = [
        # Portable-инсталлятор calibre раскладывается в calibry/Calibre/
        # (иногда прямо в calibry/) — проверяем оба варианта вложенности.
        os.path.join(_runjson_root(), 'calibry', 'Calibre', 'Calibre Portable', 'Calibre'),
        os.path.join(_runjson_root(), 'calibry', 'Calibre', 'Calibre Portable', 'Calibre', 'app'),
        os.path.join(_runjson_root(), 'calibry', 'Calibre Portable', 'Calibre'),
        os.path.join(_runjson_root(), 'calibry', 'Calibre Portable', 'Calibre', 'app'),
        os.path.join(_runjson_root(), 'calibry', 'Calibre', 'Calibre'),
        os.path.join(_runjson_root(), 'calibry', 'Calibre'),
        os.path.join(_runjson_root(), 'calibry'),
    ]
    # Выбираем не просто существующий каталог, а тот, где реально лежат
    # CLI-инструменты: иначе calibry/Calibre (пустая обёртка) выигрывала
    # у calibry/Calibre Portable/Calibre с ebook-convert.exe внутри.
    def _has_cli(d):
        return any(os.path.isfile(os.path.join(d, n + '.exe'))
                   or os.path.isfile(os.path.join(d, n))
                   for n in ('calibre', 'ebook-convert'))
    for c in cands:
        if os.path.isdir(c) and _has_cli(c):
            return c
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


def _is_pure_linux() -> bool:
    """True, если текущий Python работает на чистом Linux (не WSL).

    Внутри AppImage сервер работает в чистом Linux, где Calibre доступен
    как AppImage (Linux) — а не как Windows-portable.
    """
    return sys.platform == 'linux' and not is_wsl()


def _appimage_path() -> str:
    """Путь к .AppImage Calibre (только чистый Linux). Иначе — ''.

    На WSL/Windows возвращает '', чтобы не ломать рабочий portable-режим.
    """
    if not _is_pure_linux():
        return ''
    root = _runjson_root()
    cands = ['', 'calibry', os.path.join('calibry', 'Calibre')]
    for rel in cands:
        folder = os.path.join(root, rel)
        try:
            for fn in os.listdir(folder):
                if fn.lower().endswith('.appimage') and ('calibre' in fn.lower()):
                    return os.path.join(folder, fn)
        except Exception:
            swallow.note('calibry_backend._appimage_path:L131')
            continue
    return ''


_APPIMAGE_LIB = ''  # глобальный каталог lib для запуска CLI из распакованного AppImage

_CLI_LAYOUTS = (
    ('calibre', 'bin'),  # JB-сборка: squashfs-root/calibre/bin/ebook-convert
    ('usr', 'bin'),      # стандартная сборка: squashfs-root/usr/bin/ebook-convert
    ('', 'bin'),         # fallback
)


def _find_cli(root: str):
    """(bin_dir, lib_dir) внутри распакованного AppImage-каталога.

    Проверяет и сам root, и вложенные root/squashfs-root / root/AppDir
    (распаковка кладёт содержимое в squashfs-root/AppDir; готовая сборка может
    положить прямо). (('', '') если нет."""
    for base in (root, os.path.join(root, 'squashfs-root'), os.path.join(root, 'AppDir')):
        for calibre_dir, bin_dir in _CLI_LAYOUTS:
            b = os.path.join(base, calibre_dir, bin_dir)
            if os.path.isfile(os.path.join(b, 'ebook-convert')):
                lib = os.path.join(base, calibre_dir, 'lib')
                if not os.path.isdir(lib):
                    lib = b
                return b, lib
    return '', ''


def _appimage_cli_dir() -> str:
    """Каталог с CLI-инструментами Calibre из распакованного AppImage.

    Приоритет:
      1. готовая распаковка в calibry/appimage_extract (быстрый путь при сборке);
      2. распаковка рядом с самим AppImage (squashfs-root);
      3. автоматическая распаковка по требованию в .calibry_cache.

    Возвращает bin-каталог; при найденном AppImage запоминает и каталог
    библиотек в глобальной _APPIMAGE_LIB (нужен для LD_LIBRARY_PATH).
    """
    global _APPIMAGE_LIB
    ai = _appimage_path()
    if not ai:
        return ''
    staged = os.path.join(_runjson_root(), 'calibry', 'appimage_extract')
    for base in (staged,
                 os.path.join(os.path.dirname(ai), 'squashfs-root'),
                 os.path.join(_cache_dir(), 'calibre_appimage')):
        b, lib = _find_cli(base)
        if b:
            if lib and os.path.isdir(lib):
                _APPIMAGE_LIB = lib
            return b
    b, lib = _extract_appimage(ai, os.path.join(_cache_dir(), 'calibre_appimage'))
    if b and lib:
        if os.path.isdir(lib):
            _APPIMAGE_LIB = lib
        return b
    return ''


def _extract_appimage(appimage: str, dest_dir: str):
    """Распаковка AppImage Calibre через --appimage-extract (без FUSE).

    Возвращает (bin_dir, lib_dir), пересчитанные относительно dest_dir (после rename).
    """
    tmp = dest_dir + '.tmp'
    try:
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        r = subprocess.run([appimage, '--appimage-extract'],
                           cwd=tmp, capture_output=True, timeout=900)
        if r.returncode != 0:
            return '', ''
        root = os.path.join(tmp, 'squashfs-root')
        b, lib = _find_cli(root)
        if not b:
            return '', ''
        # пересчитать пути относительно dest_dir (переименование tmp → dest)
        b = b.replace(tmp, dest_dir, 1)
        lib = lib.replace(tmp, dest_dir, 1)
        os.rename(tmp, dest_dir)
        return b, lib
    except Exception:
        swallow.note('calibry_backend._extract_appimage:L218')
        return '', ''
    finally:
        if os.path.exists(tmp):
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                swallow.note('calibry_backend._extract_appimage:L225')
                pass


def _tool_exe(name: str) -> str:
    """Полный путь к инструменту Calibre.

    На чистом Linux (AppImage/сборка) приоритет — распакованный AppImage Calibre
    (CLI-инструменты без .exe). На WSL/Windows — портабельный portable c .exe.
    """
    if _is_pure_linux():
        cli = _appimage_cli_dir()
        if cli:
            p = os.path.join(cli, name)
            if os.path.exists(p):
                return p
    d = get_calibre_dir()
    exe = name + ('.exe' if is_wsl() or sys.platform == 'win32' else '')
    p = os.path.join(d, exe)
    if sys.platform == 'win32' and not os.path.exists(p):
        p = os.path.join(d, name)
    return p


def _posix_to_win(p: str) -> str:
    """/mnt/c/foo или /mnt/e/foo → C:\\foo / E:\\foo."""
    if not p:
        return p
    p = p.replace('\\', '/')
    m = re.match(r'^/mnt/([a-zA-Z])/(.*)$', p)
    if m:
        return m.group(1).upper() + ':\\' + m.group(2).replace('/', '\\')
    m = re.match(r'^/mnt/wsl/([^/]+)/(.*)$', p)
    if m:
        return '\\\\wsl.localhost\\' + m.group(1) + '\\' + m.group(2).replace('/', '\\')
    return p


def _win_to_posix(p: str) -> str:
    """C:\\foo или E:\\foo → /mnt/c/foo /mnt/e/foo."""
    if not p:
        return p
    p = p.replace('\\', '/')
    m = re.match(r'^([a-zA-Z]):/(.*)$', p)
    if m:
        return '/mnt/' + m.group(1).lower() + '/' + m.group(2)
    m = re.match(r'^//wsl[.]localhost/([^/]+)/(.*)$', p)
    if m:
        return '/mnt/wsl/' + m.group(1) + '/' + m.group(2)
    return p


def _win_visible(p: str) -> bool:
    """Может ли Windows-процесс Calibre напрямую открыть этот путь?"""
    if not p:
        return False
    p = p.replace('\\', '/')
    if sys.platform == 'win32':
        return True  # на Windows пути уже локальные, exe-вызовы локальные
    if is_wsl():
        # /mnt/<буква>/... — это Windows-диски; остальное (bigstorage, /home...) — stage
        m = re.match(r'^/mnt/([a-zA-Z])/', p)
        return bool(m)
    return True


def _cache_dir() -> str:
    d = os.path.join(_runjson_root(), '.calibry_cache')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        swallow.note('calibry_backend._cache_dir:L296')
        pass
    return d


def _stage_input(fp: str):
    """Если файл не виден Windows-процессу — копируем в кэш.

    Возвращает (work_path_posix, is_temp: bool). is_temp=True → файл нужно удалить.
    """
    if _win_visible(fp):
        return fp, False
    ext = os.path.splitext(fp)[1] or '.book'
    h = hashlib.sha1(fp.encode('utf-8', 'replace')).hexdigest()[:16]
    dst = os.path.join(_cache_dir(), 'in_' + h + ext)
    try:
        if not os.path.exists(dst) or os.path.getmtime(dst) != os.path.getmtime(fp):
            shutil.copy2(fp, dst)
    except Exception as e:
        swallow.note('calibry_backend._stage_input:L315', e)
        return fp, False  # не смогли скопировать — попробуем как есть (UNC)
    return dst, True


def _run(tool: str, args, timeout=120, cwd=None):
    """Запуск инструмента Calibre. Возвращает (code, stdout, stderr)."""
    exe = _tool_exe(tool)
    if not os.path.exists(exe):
        raise FileNotFoundError(f"Calibre {tool} не найден: {exe}")

    # аргументы: пути → Windows-вид (и WSL, и Windows)
    win_args = []
    for a in args:
        if isinstance(a, str) and ('/' in a or '\\' in a) and (
                a.startswith('/mnt/') or re.match(r'^[a-zA-Z]:[\\/]', a) or a.startswith('\\\\')):
            win_args.append(_posix_to_win(a) if (a.startswith('/mnt/') or (sys.platform == 'win32' and a.startswith('/'))) else a)
        else:
            win_args.append(a)

    a = [exe] + win_args
    cwd = cwd or _cache_dir()
    env = None
    # На чистом Linux CLI Calibre из распакованного AppImage требует свои .so
    if _is_pure_linux() and _APPIMAGE_LIB and os.path.isdir(_APPIMAGE_LIB):
        env = dict(os.environ)
        libpath = _APPIMAGE_LIB
        if env.get('LD_LIBRARY_PATH'):
            libpath = _APPIMAGE_LIB + os.pathsep + env['LD_LIBRARY_PATH']
        env['LD_LIBRARY_PATH'] = libpath
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    p = subprocess.run(a, capture_output=True, timeout=timeout, cwd=cwd, env=env, **kw)
    out = p.stdout.decode('utf-8', errors='replace')
    err = p.stderr.decode('utf-8', errors='replace')
    return p.returncode, out, err


def _read_epub_simple(epub_fp):
    """Паушальный читатель EPUB без зависимостей (ebooklib/bs4 отсутствуют)."""
    import zipfile
    from html import unescape
    try:
        zf = zipfile.ZipFile(epub_fp)
    except Exception as e:
        swallow.note('calibry_backend._read_epub_simple:L359', e)
        return {'error': f'EPUB: {e}'}
    # порядок чтения из container/spine
    order = []
    try:
        def _href(m):
            return unescape(m.group(1))
        import re as _re
        for name in zf.namelist():
            if name.endswith('.opf'):
                content = zf.read(name).decode('utf-8', 'replace')
                ids = {}
                for mm in _re.finditer(r'<item\s+[^>]*id=["\']([^"\']+)["\'][^>]*href=["\']([^"\']+)["\']', content):
                    ids[mm.group(1)] = mm.group(2)
                for mm in _re.finditer(r'<itemref\s+[^>]*idref=["\']([^"\']+)["\']', content):
                    h = ids.get(mm.group(1))
                    if h:
                        order.append(h.split('#')[0])
                break
    except Exception:
        swallow.note('calibry_backend._read_epub_simple:L379')
        order = []
    seen = set()
    chapters = []
    for name in zf.namelist():
        ln = name.lower()
        if not (ln.endswith('.xhtml') or ln.endswith('.html') or ln.endswith('.htm')):
            continue
        rel = name.split('/')[-1]
        if order and rel not in order:
            continue
        if name in seen:
            continue
        seen.add(name)
        try:
            html = zf.read(name).decode('utf-8', 'replace')
        except Exception:
            swallow.note('calibry_backend._read_epub_simple:L396')
            continue
        text = unescape(re.sub(r'<[^>]+>', '\n', html))
        text = '\n'.join(l.strip() for l in text.splitlines() if l.strip())
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if text:
            chapters.append({'title': rel, 'text': text})
    if not chapters:
        return {'error': 'EPUB: не удалось извлечь текст'}
    return {'title': os.path.basename(epub_fp), 'author': '', 'chapters': chapters, 'format': 'epub'}


def _clean_cache(max_files=400, max_age_days=7):
    """Ограничиваем размер кэша — удаляем самые старые файлы."""
    try:
        with _lock:
            files = glob.glob(os.path.join(_cache_dir(), '*'))
            if len(files) <= max_files:
                return
            files.sort(key=lambda f: os.path.getmtime(f))
            cutoff = time.time() - max_age_days * 86400
            for f in files:
                if len(files) <= max_files:
                    break
                if os.path.isfile(f) and (os.path.getmtime(f) < cutoff or True):
                    try:
                        os.unlink(f)
                        files.remove(f)
                    except Exception:
                        swallow.note('calibry_backend._clean_cache:L425')
                        pass
    except Exception:
        swallow.note('calibry_backend._clean_cache:L428')
        pass


# ─────────────────────────── статус / форматирование ───────────────────────────

_VERSION_CACHE = None
CALIBRE_AVAILABLE = None


def calibre_available() -> bool:
    global CALIBRE_AVAILABLE
    if CALIBRE_AVAILABLE is None:
        CALIBRE_AVAILABLE = os.path.exists(_tool_exe('ebook-convert'))
    return CALIBRE_AVAILABLE


def calibre_version():
    global _VERSION_CACHE
    if _VERSION_CACHE is not None:
        return _VERSION_CACHE
    try:
        code, out, err = _run('ebook-convert', ['--version'], timeout=30)
        _VERSION_CACHE = (out or err).strip().splitlines()[0] if (out or err) else 'unknown'
    except Exception as e:
        swallow.note('calibry_backend.calibre_version:L453', e)
        _VERSION_CACHE = f'error: {e}'
    return _VERSION_CACHE


CALIBRE_INPUT_EXTS = [
    'azw', 'azw1', 'azw3', 'azw4', 'cb7', 'cbc', 'cbr', 'cbz', 'chm', 'docx',
    'epub', 'fb2', 'fbz', 'html', 'htm', 'htmlz', 'imp', 'kepub', 'lit',
    'lrf', 'lrx', 'mobi', 'odt', 'oebzip', 'opf', 'pdb', 'pdf', 'pml',
    'pmlz', 'pobi', 'prc', 'rb', 'rtf', 'snb', 'tcr', 'tpz', 'txt', 'txtz',
    'updb',
]

CALIBRE_OUTPUT_EXTS = [
    'azw3', 'docx', 'epub', 'fb2', 'htmlz', 'kepub', 'lrf', 'mobi', 'oeb',
    'pdb', 'pdf', 'pml', 'rb', 'rtf', 'snb', 'tcr', 'txt', 'txtz',
]

# Форматы, которые наша читалка НЕ читает нативно, но читает Calibre
CALIBRY_READ_EXTS = {
    '.azw', '.azw1', '.azw3', '.azw4', '.cb7', '.cbc', '.cbr', '.cbz',
    '.chm', '.fbz', '.html', '.htm', '.htmlz', '.imp', '.kepub', '.lit',
    '.lrf', '.lrx', '.mobi', '.odt', '.oebzip', '.opf', '.pdb', '.pml',
    '.pmlz', '.pobi', '.prc', '.rb', '.snb', '.tcr', '.tpz', '.txtz',
    '.updb',
}


def formats_info() -> dict:
    return {
        'available': calibre_available(),
        'version': calibre_version(),
        'input': CALIBRE_INPUT_EXTS,
        'output': CALIBRE_OUTPUT_EXTS,
        'native_readable': ['.txt', '.epub', '.fb2', '.pdf', '.docx', '.doc', '.rtf', '.djvu', '.zip'],
    }


# ─────────────────────────── конвертация ───────────────────────────

def convert_book(src_fp, target_ext='epub', out_fp=None, extra_args=None,
                 timeout=900, save_next_to=None):
    """Конвертация книги через ebook-convert.

    src_fp      — путь на стороне текущей ОС (WSL или Windows)
    target_ext  — целевой формат (epub, mobi, txt, fb2, ...)
    out_fp      — желаемый путь результата (иначе кэш)
    save_next_to — при True/папке результат кладём рядом с исходником
    Возвращает dict: {ok, out_path, out_wsl, error, log}
    """
    if not calibre_available():
        return {'ok': False, 'error': 'Calibre не найден. Поместите AppImage Calibre в папку calibry/ (или портабельный Calibre на WSL/Windows)'}
    if not os.path.exists(src_fp):
        return {'ok': False, 'error': f'Файл не найден: {src_fp}'}

    ext = target_ext.lstrip('.')
    if ext not in CALIBRE_OUTPUT_EXTS:
        return {'ok': False, 'error': f'Целевой формат "{ext}" Calibre не поддерживает для вывода'}

    work, is_temp = _stage_input(src_fp)
    try:
        if out_fp is None:
            if save_next_to:
                base = os.path.join(os.path.dirname(src_fp),
                                    os.path.splitext(os.path.basename(src_fp))[0] + '.' + ext)
                out_fp = base
                if os.path.exists(out_fp):
                    out_fp = base  # перезапишем при конвертации
            else:
                h = hashlib.sha1((src_fp + ':' + str(os.path.getmtime(src_fp)) + ':' + ext).encode('utf-8', 'replace')).hexdigest()[:16]
                out_fp = os.path.join(_cache_dir(), f'conv_{h}.{ext}')
        out_win = _posix_to_win(out_fp)

        args = [_posix_to_win(work), out_win]
        if extra_args:
            args += [str(x) for x in extra_args]

        with _lock:
            code, out, err = _run('ebook-convert', args, timeout=timeout, cwd=_cache_dir())
        if code != 0 or not os.path.exists(out_fp):
            tail = (err or out or '').strip()[-800:]
            return {'ok': False, 'error': f'Конвертация не удалась (код {code}): {tail}'}
        _clean_cache()
        return {'ok': True, 'out_path': out_fp, 'out_wsl': out_fp, 'log': (out or '').strip()[-400:]}
    except Exception as e:
        swallow.note('calibry_backend.convert_book:L538', e)
        return {'ok': False, 'error': str(e)}
    finally:
        if is_temp and os.path.exists(work):
            try:
                os.unlink(work)
            except Exception:
                swallow.note('calibry_backend.convert_book:L545')
                pass


def read_via_calibry(fp, ext):
    """Прочитать книгу, конвертировав её в EPUB через Calibre (с кэшем).

    Возвращает структуру как у _read_epub (title/author/chapters/...):
    """
    ext = ext.lower()
    target = 'epub'
    h = hashlib.sha1((fp + ':' + str(os.path.getmtime(fp))).encode('utf-8', 'replace')).hexdigest()[:16]
    cached = os.path.join(_cache_dir(), f'read_{h}.epub')
    if not os.path.exists(cached):
        res = convert_book(fp, target, out_fp=cached, timeout=900)
        if not res.get('ok'):
            return {'error': res.get('error', 'Конвертация не удалась')}
    try:
        import server_backup as _sb
        data = _sb._read_epub(cached)
        if 'error' in data:
            data = _read_epub_simple(cached)
        if data.get('format') is None:
            data['format'] = ext.lstrip('.')
        return data
    except Exception as e:
        swallow.note('calibry_backend.read_via_calibry:L571', e)
        return {'error': f'Ошибка чтения результата Calibre: {e}'}


# ─────────────────────────── метаданные ───────────────────────────

def _parse_meta_text(text: str) -> dict:
    """Парсинг вывода ebook-meta ("Title               : значение")."""
    meta = {}
    lines = [l.rstrip() for l in text.splitlines()]
    cur_key = None
    cur_val = []
    for line in lines:
        if not line.strip():
            if cur_key:
                meta[cur_key] = '\n'.join(cur_val).strip()
                cur_key = None
                cur_val = []
            continue
        # новая запись: строка без отступа с разделителем "ключ : значение"
        # (продолжения многострочных значений идут с отступом)
        m = re.match(r'^\s*([^:]{1,60}?)\s*:\s*(.*)$', line)
        if m and not line.startswith(' ') and not line.startswith('\t'):
            if cur_key:
                meta[cur_key] = '\n'.join(cur_val).strip()
            cur_key = m.group(1).strip()
            cur_val = [m.group(2)]
        elif cur_key:
            cur_val.append(line.strip())
    if cur_key:
        meta[cur_key] = '\n'.join(cur_val).strip()
    return meta


def get_metadata(fp) -> dict:
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    work, is_temp = _stage_input(fp)
    try:
        with _lock:
            code, out, err = _run('ebook-meta', [_posix_to_win(work)], timeout=120)
        if code != 0:
            return {'error': (err or out or '').strip()[-400:]}
        meta = _parse_meta_text(out)
        meta['file_size'] = os.path.getsize(fp)
        return {'ok': True, 'metadata': meta}
    except Exception as e:
        swallow.note('calibry_backend.get_metadata:L618', e)
        return {'error': str(e)}
    finally:
        if is_temp and os.path.exists(work):
            try:
                os.unlink(work)
            except Exception:
                swallow.note('calibry_backend.get_metadata:L625')
                pass


_META_FLAGS = {
    'title': '-t', 'authors': '-a', 'comments': '-c', 'publisher': '-p',
    'series': '-s', 'series_index': '-i', 'rating': '-r', 'tags': '--tags',
    'language': '-l', 'pubdate': '-d', 'isbn': '--isbn',
    'identifier': '--identifier', 'book_producer': '-k',
}


def set_metadata(fp, fields: dict) -> dict:
    """Редактирование метаданных через ebook-meta. fields — словарь полей."""
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    work, is_temp = _stage_input(fp)
    cover_tmp = None
    try:
        args = [_posix_to_win(work)]
        for key, val in fields.items():
            if val is None:
                continue
            # Пустая строка — это команда «очистить поле»: пользователь стёр
            # автора/жанр в модалке. Числовые и датовые флаги Calibre пустых
            # значений не принимают, их пропускаем.
            if val == '' and key in ('series_index', 'rating', 'pubdate'):
                continue
            flag = _META_FLAGS.get(key)
            if key == 'cover':
                cover_tmp = os.path.join(_cache_dir(), 'cov_' + hashlib.sha1(os.urandom(6)).hexdigest()[:10] + os.path.splitext(val)[1] or '.jpg')
                try:
                    shutil.copy2(val, cover_tmp)
                except Exception as e:
                    swallow.note('calibry_backend.set_metadata:L654', e)
                    return {'error': f'Не удалось скопировать обложку: {e}'}
                cover_tmp_win = _posix_to_win(cover_tmp)
                args += ['--cover', cover_tmp_win]
            elif flag:
                args += [flag, str(val)]
        if len(args) == 1:
            return {'error': 'Нет полей для обновления'}
        with _lock:
            code, out, err = _run('ebook-meta', args, timeout=300)
        if code != 0:
            return {'error': (err or out or '').strip()[-400:]}
        if is_temp:
            shutil.copy2(work, fp)
        return {'ok': True, 'log': (out or '').strip()[-300:]}
    except Exception as e:
        swallow.note('calibry_backend.set_metadata:L670', e)
        return {'error': str(e)}
    finally:
        if is_temp and os.path.exists(work):
            try:
                os.unlink(work)
            except Exception:
                swallow.note('calibry_backend.set_metadata:L677')
                pass
        if cover_tmp and os.path.exists(cover_tmp):
            try:
                os.unlink(cover_tmp)
            except Exception:
                swallow.note('calibry_backend.set_metadata:L683')
                pass


# ─────────────────────────── полировка ───────────────────────────

POLISH_OPTIONS = {
    'smarten_punctuation': '--smarten-punctuation',
    'embed_fonts': '--embed-fonts',
    'subset_fonts': '--subset-fonts',
    'remove_unused_css': '--remove-unused-css',
    'jacket': '--jacket',
    'remove_jacket': '--remove-jacket',
    'upgrade_internals': '--upgrade-internals',
}


def polish_book(fp, options: list, save=False) -> dict:
    """Полировка EPUB/AZW3/KEPUB через ebook-polish.

    options — список ключей из POLISH_OPTIONS. save=True → заменяем оригинал.
    """
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    ext = os.path.splitext(fp)[1].lower()
    if ext not in ('.epub', '.azw3', '.kepub', '.docx'):
        return {'error': f'Полировка поддерживается только для EPUB/AZW3/KEPUB, а не "{ext}"'}
    work, is_temp = _stage_input(fp)
    try:
        out_fp = os.path.join(_cache_dir(), 'pol_' + hashlib.sha1(os.urandom(8)).hexdigest()[:12] + ext)
        args = [_posix_to_win(work), _posix_to_win(out_fp)]
        for opt in options:
            flag = POLISH_OPTIONS.get(opt)
            if flag:
                args.append(flag)
        with _lock:
            code, out, err = _run('ebook-polish', args, timeout=900, cwd=_cache_dir())
        if code != 0 or not os.path.exists(out_fp):
            return {'error': (err or out or '').strip()[-400:]}
        if save:
            shutil.copy2(out_fp, fp)
            result_fp = fp
        else:
            result_fp = out_fp
        _clean_cache()
        return {'ok': True, 'out_path': result_fp, 'log': (out or '').strip()[-300:]}
    except Exception as e:
        swallow.note('calibry_backend.polish_book:L730', e)
        return {'error': str(e)}
    finally:
        if is_temp and os.path.exists(work):
            try:
                os.unlink(work)
            except Exception:
                swallow.note('calibry_backend.polish_book:L737')
                pass


# ─────────────────────────── обложка ───────────────────────────

def extract_cover(fp, out_fp=None) -> dict:
    """Извлечь обложку книги в PNG/JPG в кэше. Возвращает путь."""
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    work, is_temp = _stage_input(fp)
    try:
        if not out_fp:
            h = hashlib.sha1((fp + ':' + str(os.path.getmtime(fp))).encode('utf-8', 'replace')).hexdigest()[:16]
            out_fp = os.path.join(_cache_dir(), f'cover_{h}.png')
        if os.path.exists(out_fp):
            return {'ok': True, 'out_path': out_fp}
        args = [_posix_to_win(work), '--get-cover', _posix_to_win(out_fp)]
        with _lock:
            code, out, err = _run('ebook-meta', args, timeout=120, cwd=_cache_dir())
        if code != 0 or not os.path.exists(out_fp) or os.path.getsize(out_fp) < 100:
            return {'error': 'Обложка не найдена в книге'}
        return {'ok': True, 'out_path': out_fp}
    except Exception as e:
        swallow.note('calibry_backend.extract_cover:L761', e)
        return {'error': str(e)}
    finally:
        if is_temp and os.path.exists(work):
            try:
                os.unlink(work)
            except Exception:
                swallow.note('calibry_backend.extract_cover:L768')
                pass


# ─────────────────────────── поиск метаданных в интернете ───────────────────────────

def fetch_metadata(title='', authors='', isbn='', timeout_sec=60) -> dict:
    """Поиск метаданных книги онлайн (Google/OpenLibrary/Amazon)."""
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    if not (title or authors or isbn):
        return {'error': 'Укажите название, автора или ISBN'}
    args = []
    if title:
        args += ['-t', title]
    if authors:
        args += ['-a', authors]
    if isbn:
        args += ['-i', isbn]
    args += ['-d', str(timeout_sec)]
    try:
        with _lock:
            code, out, err = _run('fetch-ebook-metadata', args, timeout=timeout_sec + 30)
        if code != 0:
            return {'error': (err or out or '').strip()[-400:] or 'Метаданные не найдены'}
        meta = _parse_meta_text(out)
        return {'ok': True, 'metadata': meta, 'log': (err or '').strip()[-300:]}
    except subprocess.TimeoutExpired:
        swallow.note('calibry_backend.fetch_metadata:L796')
        return {'error': 'Таймаут поиска метаданных'}
    except Exception as e:
        swallow.note('calibry_backend.fetch_metadata:L799', e)
        return {'error': str(e)}


# ─────────────────────────── редактирование исходников книги ───────────────────────────

EDIT_READABLE_EXTS = ('.fb2', '.txt', '.html', '.htm', '.xml', '.css')


def _decode_source(data):
    for enc in ('utf-8-sig', 'utf-8', 'cp1251', 'latin-1'):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            swallow.note('calibry_backend._decode_source:L813')
            continue
    return data.decode('utf-8', errors='replace')


def _backup_original(fp) -> str:
    """Копия оригинала в кэш перед изменением файла. Возвращает путь копии."""
    try:
        bdir = os.path.join(_cache_dir(), 'edit_backup')
        os.makedirs(bdir, exist_ok=True)
        name = os.path.basename(fp) + '.' + time.strftime('%Y%m%d-%H%M%S') + '.bak'
        dst = os.path.join(bdir, name)
        shutil.copy2(fp, dst)
        return dst
    except Exception:
        swallow.note('calibry_backend._backup_original:L828')
        return ''


def _rewrite_zip_entries(src_fp, replacer):
    """Пересборка zip-архива: для каждой записи replacer(name, bytes) возвращает
    новые байты (None — без изменений, _EPUB_REMOVE — удалить запись).
    mimetype пишется без сжатия. Возвращает список изменённых имён."""
    import zipfile
    changed = []
    tmp = src_fp + '.calibry_tmp'
    try:
        with zipfile.ZipFile(src_fp) as zin:
            items = [(i.filename, i.compress_type) for i in zin.infolist()]
            with zipfile.ZipFile(tmp, 'w', allowZip64=True) as zout:
                for name, ctype in items:
                    data = zin.read(name)
                    nd = replacer(name, data)
                    if nd is _EPUB_REMOVE:
                        changed.append(name)
                        continue
                    if nd is not None and nd != data:
                        changed.append(name)
                        data = nd
                    if name == 'mimetype':
                        zi = zipfile.ZipInfo('mimetype')
                        zout.writestr(zi, data)
                    else:
                        zout.writestr(name, data, compress_type=ctype)
        os.replace(tmp, src_fp)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            swallow.note('calibry_backend._rewrite_zip_entries:L862')
            pass
        raise
    return changed


_EPUB_REMOVE = '__EPUB_REMOVE__'


def read_source_text(fp, part=None) -> dict:
    """Исходный текст книги для редактора.

    fb2/txt/html — целиком; epub — список файлов глав или содержимое part;
    collection:// — исходник fb2 из архива.
    """
    if fp.startswith('collection://'):
        return _collection_read_source(fp)
    ext = os.path.splitext(fp)[1].lower()
    try:
        if ext == '.epub':
            if part:
                return _read_epub_part(fp, part)
            return _epub_sources(fp)
        if ext not in EDIT_READABLE_EXTS:
            return {'error': f'Формат "{ext}" не редактируется напрямую. Сконвертируйте книгу в fb2 или txt через «Конвертировать».',
                    'editable': False}
        with open(fp, 'rb') as f:
            raw = f.read()
        if ext != '.txt':
            import xml.etree.ElementTree as ET
            try:
                ET.fromstring(raw)
            except Exception as e:
                swallow.note('calibry_backend.read_source_text:L895', e)
                return {'error': f'Файл повреждён (не валидный XML): {e}', 'editable': False}
        return {'ok': True, 'editable': True, 'ext': ext[1:], 'text': _decode_source(raw)}
    except Exception as e:
        swallow.note('calibry_backend.read_source_text:L899', e)
        return {'error': str(e), 'editable': False}


def _epub_sources(fp):
    """Список файлов глав EPUB в порядке чтения (из spine)."""
    import zipfile
    try:
        zf = zipfile.ZipFile(fp)
    except Exception as e:
        swallow.note('calibry_backend._epub_sources:L909', e)
        return {'error': f'EPUB повреждён: {e}', 'editable': False}
    order = []
    try:
        for name in zf.namelist():
            if name.lower().endswith('.opf'):
                content = zf.read(name).decode('utf-8', 'replace')
                manifest = {}
                for mm in re.finditer(r'<item\s+[^>]*id=["\']([^"\']+)["\'][^>]*href=["\']([^"\']+)["\']', content):
                    href = mm.group(2).split('#')[0]
                    manifest[mm.group(1)] = os.path.normpath(os.path.join(os.path.dirname(name), href))
                for mm in re.finditer(r'<itemref\s+[^>]*idref=["\']([^"\']+)["\']', content):
                    h = manifest.get(mm.group(1))
                    if h:
                        order.append(h)
                break
    except Exception:
        swallow.note('calibry_backend._epub_sources:L926')
        order = []
    files = []
    seen = set()
    for name in order:
        if name in zf.namelist() and name not in seen:
            seen.add(name)
            files.append({'name': name, 'label': name.split('/')[-1]})
    if not files:
        for name in zf.namelist():
            ln = name.lower()
            if (ln.endswith('.xhtml') or ln.endswith('.html') or ln.endswith('.htm')) and name not in seen:
                seen.add(name)
                files.append({'name': name, 'label': name.split('/')[-1]})
    if not files:
        return {'error': 'В EPUB не найдены файлы глав', 'editable': False}
    return {'ok': True, 'editable': True, 'ext': 'epub', 'epub_files': files, 'text': ''}


def _read_epub_part(fp, part):
    if not part:
        return {'error': 'Не указан файл главы'}
    import zipfile
    try:
        with zipfile.ZipFile(fp) as zf:
            raw = zf.read(part)
    except KeyError:
        swallow.note('calibry_backend._read_epub_part:L953')
        return {'error': f'Файл "{part}" не найден в EPUB'}
    except Exception as e:
        swallow.note('calibry_backend._read_epub_part:L956', e)
        return {'error': f'EPUB повреждён: {e}'}
    return {'ok': True, 'editable': True, 'ext': 'epub', 'part': part, 'text': _decode_source(raw)}


def write_source_text(fp, text, part=None) -> dict:
    """Запись отредактированного текста.

    fb2/txt/html — напрямую в файл (с бэкапом оригинала);
    epub — пересборка zip с заменой файла part;
    collection:// — пересборка архива с заменой записи.
    """
    if fp.startswith('collection://'):
        return _collection_write_source(fp, text)
    ext = os.path.splitext(fp)[1].lower()
    try:
        if ext == '.epub':
            return _write_epub_part(fp, part, text)
        if ext not in EDIT_READABLE_EXTS:
            return {'error': f'Формат "{ext}" не редактируется напрямую'}
        if ext != '.txt':
            import xml.etree.ElementTree as ET
            try:
                ET.fromstring(text.encode('utf-8'))
            except Exception as e:
                swallow.note('calibry_backend.write_source_text:L981', e)
                return {'error': f'XML не валиден — сохранить нельзя: {e}'}
        backup = _backup_original(fp)
        tmp = fp + '.calibry_tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(text)
            os.replace(tmp, fp)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                swallow.note('calibry_backend.write_source_text:L993')
                pass
            raise
        return {'ok': True, 'backup_path': backup, 'ext': ext[1:]}
    except Exception as e:
        swallow.note('calibry_backend.write_source_text:L998', e)
        return {'error': str(e)}


def _write_epub_part(fp, part, text):
    if not part:
        return {'error': 'Не указан файл главы EPUB'}
    if part.lower().endswith(('.xhtml', '.html', '.htm', '.xml', '.opf', '.ncx')):
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(text.encode('utf-8'))
        except Exception as e:
            swallow.note('calibry_backend._write_epub_part:L1010', e)
            return {'error': f'XML не валиден — сохранить нельзя: {e}'}
    new_bytes = text.encode('utf-8')
    checked = []

    def replacer(name, data):
        if name == part:
            checked.append(name)
            return new_bytes
        return None
    try:
        backup = _backup_original(fp)
        changed = _rewrite_zip_entries(fp, replacer)
    except Exception as e:
        swallow.note('calibry_backend._write_epub_part:L1024', e)
        return {'error': str(e)}
    if not changed:
        return {'error': 'Файл главы не найден в EPUB'}
    return {'ok': True, 'backup_path': backup, 'ext': 'epub'}


# ──────────────────────── книги из коллекций (zip-архивы) ────────────────────────

def _collection_resolve(fp):
    """`collection://ID` → (archive_path, entry_path, book_id)."""
    book_id = fp.replace('collection://', '', 1)
    try:
        book_id = int(book_id)
    except ValueError:
        raise ValueError('Некорректный ID книги коллекции')
    try:
        from modules.collection_module import get_book as _cget_book, find_archive
    except Exception:
        try:
            from collection_module import get_book as _cget_book, find_archive
        except Exception as e:
            raise RuntimeError(f'Модуль коллекций недоступен: {e}')
    import server_backup as _sb
    book = _cget_book(book_id)
    if not book:
        raise ValueError('Книга коллекции не найдена')
    archive = book.get('archive_filename')
    entry = book.get('file_path')
    if not archive or not entry:
        raise ValueError('Архив книги не найден')
    archive_path = find_archive(archive, getattr(_sb, 'BOOKS_DIR', None))
    if not archive_path or not os.path.exists(archive_path):
        raise ValueError(f'Архив не найден: {archive}')
    return archive_path, entry, book_id


def _collection_read_source(fp) -> dict:
    try:
        archive_path, entry, book_id = _collection_resolve(fp)
    except Exception as e:
        swallow.note('calibry_backend._collection_read_source:L1065', e)
        return {'error': str(e), 'editable': False}
    import zipfile
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            if entry not in names:
                for name in names:
                    if name.lower().endswith(('.fb2', '.fb2.zip')):
                        entry = name
                        break
                else:
                    return {'error': 'FB2-файл не найден в архиве', 'editable': False}
            raw = zf.read(entry)
            if entry.lower().endswith('.fb2.zip'):
                with zipfile.ZipFile(io.BytesIO(raw)) as inner:
                    raw = inner.read(inner.namelist()[0])
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(raw)
        except Exception as e:
            swallow.note('calibry_backend._collection_read_source:L1086', e)
            return {'error': f'Книга повреждена (не валидный XML): {e}', 'editable': False}
    except Exception as e:
        swallow.note('calibry_backend._collection_read_source:L1089', e)
        return {'error': str(e), 'editable': False}
    return {'ok': True, 'editable': True, 'ext': 'fb2', 'text': _decode_source(raw),
            'collection': True, 'part': entry}


def _collection_write_source(fp, text) -> dict:
    try:
        archive_path, entry, book_id = _collection_resolve(fp)
    except Exception as e:
        swallow.note('calibry_backend._collection_write_source:L1099', e)
        return {'error': str(e)}
    import zipfile
    try:
        import xml.etree.ElementTree as ET
        ET.fromstring(text.encode('utf-8'))
    except Exception as e:
        swallow.note('calibry_backend._collection_write_source:L1106', e)
        return {'error': f'XML не валиден — сохранить нельзя: {e}'}
    new_bytes = text.encode('utf-8')
    nested = entry.lower().endswith('.fb2.zip')

    def replacer(name, data):
        if name != entry:
            return None
        if not nested:
            return new_bytes
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                inner_items = [(i.filename, i.compress_type) for i in inner.infolist()]
                if len(inner_items) != 1:
                    return data
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w', allowZip64=True) as oi:
                    oi.writestr(inner_items[0][0], new_bytes, compress_type=inner_items[0][1])
                return buf.getvalue()
        except Exception:
            swallow.note('calibry_backend.replacer:L1126')
            return data
    try:
        backup = _backup_original(archive_path)
        changed = _rewrite_zip_entries(archive_path, replacer)
    except Exception as e:
        swallow.note('calibry_backend._collection_write_source:L1132', e)
        return {'error': str(e)}
    if not changed:
        return {'error': 'Запись книги не найдена в архиве'}
    _collection_touch_size(book_id, len(new_bytes))
    return {'ok': True, 'backup_path': backup,
            'archive': os.path.basename(archive_path), 'collection': True}


def _collection_touch_size(book_id, new_size):
    """Best-effort обновление file_size в БД коллекции (БД может быть RO)."""
    try:
        from modules import collection_module as cm
    except Exception:
        swallow.note('calibry_backend._collection_touch_size:L1146')
        return
    db = getattr(cm, 'COLLECTION_DB', '') or getattr(cm, '_DEFAULT_DB', '')
    if not db or db.startswith('file://'):
        return
    if not db.startswith('/') and re.match(r'^[a-zA-Z]:', db):
        db = _win_to_posix(db)
    try:
        import sqlite3
        conn = sqlite3.connect(db, timeout=3)
        try:
            conn.execute("UPDATE books SET file_size=? WHERE id=?", (new_size, book_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        swallow.note('calibry_backend._collection_touch_size:L1162')
        pass


def extract_collection_cover(fp, out_fp=None) -> dict:
    """Обложка книги из архива коллекции (entry → temp → extract_cover)."""
    try:
        archive_path, entry, book_id = _collection_resolve(fp)
    except Exception as e:
        swallow.note('calibry_backend.extract_collection_cover:L1171', e)
        return {'error': str(e)}
    if not out_fp:
        h = hashlib.sha1(f'{book_id}:{os.path.getmtime(archive_path)}'.encode('utf-8', 'replace')).hexdigest()[:16]
        out_fp = os.path.join(_cache_dir(), f'cover_coll_{h}.png')
    if os.path.exists(out_fp):
        return {'ok': True, 'out_path': out_fp}
    import zipfile
    tmp = None
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            if entry not in names:
                for name in names:
                    if name.lower().endswith(('.fb2', '.fb2.zip')):
                        entry = name
                        break
                else:
                    return {'error': 'FB2-запись не найдена в архиве'}
            raw = zf.read(entry)
            if entry.lower().endswith('.fb2.zip'):
                with zipfile.ZipFile(io.BytesIO(raw)) as inner:
                    raw = inner.read(inner.namelist()[0])
        tmp = os.path.join(_cache_dir(), 'ext_' + hashlib.sha1(os.urandom(8)).hexdigest()[:12] + '.fb2')
        with open(tmp, 'wb') as f:
            f.write(raw)
        return extract_cover(tmp, out_fp=out_fp)
    except Exception as e:
        swallow.note('calibry_backend.extract_collection_cover:L1199', e)
        return {'error': str(e)}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                swallow.note('calibry_backend.extract_collection_cover:L1206')
                pass


# ─────────────────────────── создание обложек ───────────────────────────

COVER_THEMES = {
    'blue':   ('#1e3a5f', '#2e5a8a'),
    'green':  ('#1f4d2e', '#2f7a48'),
    'red':    ('#5f1e24', '#8a2f38'),
    'purple': ('#3d1f5f', '#5d2f8a'),
    'teal':   ('#0f4c52', '#17707a'),
    'dark':   ('#1c1c1e', '#38383d'),
}

_COVER_PS = r'''param([string]$Spec,[string]$Out)
$ErrorActionPreference='Stop'
try{
  Add-Type -AssemblyName System.Drawing
  $spec=Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json
  $title=[string]$spec.title
  $author=[string]$spec.author
  $c1=[string]$spec.c1
  $c2=[string]$spec.c2
  $fmt=[string]$spec.fmt
  $W=600;$H=800
  $bmp=New-Object System.Drawing.Bitmap($W,$H)
  $g=[System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode=[System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.TextRenderingHint=[System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
  $top=[System.Drawing.ColorTranslator]::FromHtml($c1)
  $bot=[System.Drawing.ColorTranslator]::FromHtml($c2)
  $rect=New-Object System.Drawing.Rectangle(0,0,$W,$H)
  $grad=New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect,$top,$bot,90.0)
  $g.FillRectangle($grad,$rect)
  $white=[System.Drawing.Brushes]::White
  $maxW=$W-140
  if($title.Length -gt 300){$title=$title.Substring(0,300)}
  $size=58.0
  $lines=@()
  do{
    $font=New-Object System.Drawing.Font('Georgia',[single]$size,[System.Drawing.FontStyle]::Bold)
    $lines=@()
    foreach($word in ($title -split ' ')){
      if($lines.Count -eq 0){$lines+=@($word);continue}
      $cand=$lines[$lines.Count-1]+' '+$word
      $sw=$g.MeasureString($cand,$font).Width
      if($sw -le $maxW){$lines[$lines.Count-1]=$cand}else{$lines+=@($word)}
    }
    $maxLine=0.0
    foreach($ln in $lines){$ml=$g.MeasureString($ln,$font).Width;if($ml -gt $maxLine){$maxLine=$ml}}
    $totalH=($lines.Count*$size*1.28)
    if(($maxLine -gt $maxW -or $totalH -gt 430) -and $size -gt 22){$size=$size-3}
    else{break}
  }while($true)
  $y=140
  foreach($ln in $lines){
    $sw=$g.MeasureString($ln,$font).Width
    $g.DrawString($ln,$font,$white,[single](($W-$sw)/2),[single]$y)
    $y+=[int]($size*1.28)
  }
  if($author){
    $y+=16
    $pen=New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(160,255,255,255),2.0)
    $g.DrawLine($pen,[single](($W/2)-70),[single]$y,[single](($W/2)+70),[single]$y)
    $y+=30
    if($author.Length -gt 70){$author=$author.Substring(0,70)+'...'}
    $fontA=New-Object System.Drawing.Font('Georgia',24,[System.Drawing.FontStyle]::Regular)
    $sw=$g.MeasureString($author,$fontA).Width
    if($sw -gt $maxW){$fontA=New-Object System.Drawing.Font('Georgia',19,[System.Drawing.FontStyle]::Regular);$sw=$g.MeasureString($author,$fontA).Width}
    $g.DrawString($author,$fontA,$white,[single](($W-$sw)/2),[single]$y)
  }
  if($fmt){
    $band=New-Object System.Drawing.Rectangle(0,$H-84,$W,84)
    $bb=New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(70,0,0,0))
    $g.FillRectangle($bb,$band)
    $fontF=New-Object System.Drawing.Font('Arial',15,[System.Drawing.FontStyle]::Bold)
    $ft=$fmt.ToUpperInvariant()
    $sw=$g.MeasureString($ft,$fontF).Width
    $g.DrawString($ft,$fontF,[System.Drawing.Brushes]::WhiteSmoke,[single](($W-$sw)/2),[single]($H-55))
  }
  $g.Dispose()
  $bmp.Save($Out,[System.Drawing.Imaging.ImageFormat]::Png)
  $bmp.Dispose()
  Write-Output 'OK'
}catch{
  Write-Error $_.Exception.Message
  exit 1
}
'''


def _ps_path():
    if is_wsl():
        return '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    return 'powershell.exe'


def generate_cover(title='', author='', theme='blue', fmt='', out_fp=None) -> dict:
    """Сгенерировать обложку: название и автор на градиентном фоне (System.Drawing)."""
    c1, c2 = COVER_THEMES.get(theme, COVER_THEMES['blue'])
    if not out_fp:
        out_fp = os.path.join(_cache_dir(), 'gen_' + hashlib.sha1(os.urandom(8)).hexdigest()[:12] + '.png')
    spec_f = os.path.join(_cache_dir(), 'spec_' + hashlib.sha1(os.urandom(6)).hexdigest()[:8] + '.json')
    try:
        with open(spec_f, 'w', encoding='utf-8') as f:
            json.dump({'title': title or 'Без названия', 'author': author or '',
                       'c1': c1, 'c2': c2, 'fmt': fmt or ''}, f, ensure_ascii=False)
        ps = os.path.join(_cache_dir(), 'gen_cover.ps1')
        if not os.path.exists(ps):
            with open(ps, 'w', encoding='utf-8') as f:
                f.write(_COVER_PS)
        if not os.path.exists(out_fp):
            with _lock:
                kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
                p = subprocess.run(
                    [_ps_path(), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                     _posix_to_win(ps), '-Spec', _posix_to_win(spec_f), '-Out', _posix_to_win(out_fp)],
                    capture_output=True, timeout=120, **kw)
            if p.returncode != 0 or not os.path.exists(out_fp) or os.path.getsize(out_fp) < 100:
                tail = (p.stderr or b'').decode('utf-8', 'replace').strip()[-300:]
                return {'error': f'Генерация обложки не удалась: {tail or p.stdout}'}
        return {'ok': True, 'out_path': out_fp}
    except Exception as e:
        swallow.note('calibry_backend.generate_cover:L1330', e)
        return {'error': str(e)}
    finally:
        try:
            os.unlink(spec_f)
        except Exception:
            swallow.note('calibry_backend.generate_cover:L1336')
            pass


def cover_from_internet(title='', author='', isbn='') -> dict:
    """Скачать обложку из Google Books (fallback OpenLibrary по ISBN)."""
    query = ''
    if isbn:
        query = 'isbn:' + isbn
    elif title and author:
        query = f'intitle:{title} inauthor:{author}'
    elif title:
        query = f'intitle:{title}'
    elif author:
        query = f'inauthor:{author}'
    if not query:
        return {'error': 'Укажите название или автора'}
    try:
        import urllib.request
        import urllib.parse
        req_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        url = 'https://www.googleapis.com/books/v1/volumes?q=' + urllib.parse.quote(query) + '&maxResults=5'
        with urllib.request.urlopen(urllib.request.Request(url, headers=req_headers), timeout=30) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
        thumb = None
        for item in data.get('items', []):
            links = (item.get('volumeInfo') or {}).get('imageLinks') or {}
            t = links.get('thumbnail') or links.get('smallThumbnail')
            if t:
                t = re.sub(r'&edge=curl', '', t).replace('http://', 'https://')
                t = re.sub(r'zoom=\d+', 'zoom=2', t)
                t = re.sub(r'&?source=[^&]+', '', t)
                thumb = t
                break
        if thumb:
            with urllib.request.urlopen(urllib.request.Request(thumb, headers=req_headers), timeout=30) as r:
                raw = r.read()
            if len(raw) > 200:
                fn = os.path.join(_cache_dir(), 'net_' + hashlib.sha1(os.urandom(8)).hexdigest()[:12] +
                                  ('.png' if raw[:8] == b'\x89PNG\r\n\x1a\n' else '.jpg'))
                with open(fn, 'wb') as f:
                    f.write(raw)
                return {'ok': True, 'out_path': fn, 'source': 'google'}
    except Exception:
        swallow.note('calibry_backend.cover_from_internet:L1380')
        pass
    if isbn:
        try:
            import urllib.request
            url = f'https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg'
            with urllib.request.urlopen(urllib.request.Request(url, headers=req_headers), timeout=30) as r:
                raw = r.read()
            if len(raw) > 200:
                fn = os.path.join(_cache_dir(), 'net_' + hashlib.sha1(os.urandom(6)).hexdigest()[:10] + '.jpg')
                with open(fn, 'wb') as f:
                    f.write(raw)
                return {'ok': True, 'out_path': fn, 'source': 'openlibrary'}
        except Exception:
            swallow.note('calibry_backend.cover_from_internet:L1394')
            pass
    return {'error': 'Обложка в интернете не найдена'}


def _collection_rewrite_entry(fp, transform) -> dict:
    """Применить transform(temp_fb2_path) к книге из архива и пересобрать архив."""
    try:
        archive_path, entry, book_id = _collection_resolve(fp)
    except Exception as e:
        swallow.note('calibry_backend._collection_rewrite_entry:L1404', e)
        return {'error': str(e)}
    import zipfile
    tmp = None
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            if entry not in names:
                for name in names:
                    if name.lower().endswith(('.fb2', '.fb2.zip')):
                        entry = name
                        break
                else:
                    return {'error': 'FB2-запись не найдена в архиве'}
            raw = zf.read(entry)
            nested = entry.lower().endswith('.fb2.zip')
            if nested:
                with zipfile.ZipFile(io.BytesIO(raw)) as inner:
                    inner_name = inner.namelist()[0]
                    raw = inner.read(inner_name)
        tmp = os.path.join(_cache_dir(), 'coll_' + hashlib.sha1(os.urandom(8)).hexdigest()[:12] + '.fb2')
        with open(tmp, 'wb') as f:
            f.write(raw)
        res = transform(tmp)
        if not isinstance(res, dict) or not res.get('ok'):
            return res if isinstance(res, dict) else {'error': str(res)}
        with open(tmp, 'rb') as f:
            new_raw = f.read()

        def replacer(name, data):
            if name != entry:
                return None
            if not nested:
                return new_raw
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                    inner_items = [(i.filename, i.compress_type) for i in inner.infolist()]
                    if len(inner_items) != 1:
                        return data
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, 'w', allowZip64=True) as oi:
                        oi.writestr(inner_items[0][0], new_raw, compress_type=inner_items[0][1])
                    return buf.getvalue()
            except Exception:
                swallow.note('calibry_backend.replacer:L1448')
                return data
        backup = _backup_original(archive_path)
        changed = _rewrite_zip_entries(archive_path, replacer)
    except Exception as e:
        swallow.note('calibry_backend._collection_rewrite_entry:L1453', e)
        return {'error': str(e)}
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                swallow.note('calibry_backend._collection_rewrite_entry:L1460')
                pass
    if not changed:
        return {'error': 'Запись книги не найдена в архиве'}
    _collection_touch_size(book_id, len(new_raw) if 'new_raw' in locals() else 0)
    return {'ok': True, 'backup_path': backup if 'backup' in locals() else '',
            'archive': os.path.basename(archive_path), 'collection': True}


def set_book_cover(fp, cover_path) -> dict:
    """Вписать обложку в файл книги (ebook-meta --cover) или в архив коллекции."""
    if not cover_path or not os.path.exists(cover_path):
        return {'error': 'Файл обложки не найден'}
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    if fp.startswith('collection://'):
        return _collection_rewrite_entry(fp, lambda tmp: set_metadata(tmp, {'cover': cover_path}))
    ext = os.path.splitext(fp)[1].lower()
    if ext not in ('.fb2', '.epub', '.mobi', '.azw3', '.azw', '.kepub', '.docx', '.pdf',
                   '.rtf', '.txt', '.html', '.htm', '.lrf', '.lit'):
        return {'error': f'Запись обложки в формат "{ext}" не поддерживается'}
    return set_metadata(fp, {'cover': cover_path})


def _el_remove(scope_el, child):
    """Удалить child из его реального родителя внутри scope_el."""
    for parent in scope_el.iter():
        if child in list(parent):
            parent.remove(child)
            return True
    return False


def _fb2_strip_cover(raw):
    """Удалить из FB2 блок обложки (<coverpage> + бинарник). None — XML битый."""
    import xml.etree.ElementTree as ET
    FB2NS = 'http://www.gribuser.ru/xml/fictionbook/2.0'
    try:
        root = ET.fromstring(raw)
    except Exception:
        swallow.note('calibry_backend._fb2_strip_cover:L1500')
        return None
    ns_candidates = ({'fb': FB2NS}, {})

    def _finds(el, path_parts):
        hits = []
        for ns in ns_candidates:
            try:
                rez = el.findall('.//' + '/'.join(path_parts), ns)
                hits.extend(rez)
            except Exception:
                swallow.note('calibry_backend._finds:L1511')
                pass
        return hits
    cover_ids = set()
    for desc in _finds(root, ['fb:description']):
        cps = desc.findall('.//fb:coverpage', ns_candidates[0])
        if not cps:
            cps = desc.findall('.//coverpage', ns_candidates[1])
        for cp in cps:
            imgs = cp.findall('fb:image', ns_candidates[0])
            if not imgs:
                imgs = cp.findall('image', ns_candidates[1])
            for img in imgs:
                href = img.get('{http://www.w3.org/1999/xlink}href') or img.get('href') or ''
                if href.startswith('#'):
                    cover_ids.add(href[1:])
            _el_remove(desc, cp)
    if not cover_ids:
        return raw
    changed = False
    for b in _finds(root, ['fb:binary']):
        if b.get('id') in cover_ids:
            _el_remove(root, b)
            changed = True
    if not changed:
        return raw
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _epub_strip_cover_rewrite(fp):
    """Удалить обложку из EPUB: чистка OPF (meta cover, cover-image item, guide) + удаление файла."""
    import zipfile
    try:
        with zipfile.ZipFile(fp) as zf:
            opf_name = next((n for n in zf.namelist() if n.lower().endswith('.opf')), None)
            if not opf_name:
                return {'error': 'OPF не найден в EPUB'}
            opf = zf.read(opf_name).decode('utf-8', 'replace')
    except Exception as e:
        swallow.note('calibry_backend._epub_strip_cover_rewrite:L1550', e)
        return {'error': f'EPUB повреждён: {e}'}
    base = os.path.dirname(opf_name)
    cover_meta = set()
    for m in re.finditer(r'<meta\s+[^>]*name=["\']cover["\'][^>]*content=["\']([^"\']+)["\']', opf, re.I):
        cover_meta.add(m.group(1))
    remove_items = {}
    exact_tags = []
    for m in re.finditer(r'<item\b[^>]*/?>', opf):
        tag = m.group(0)
        mid = re.search(r'id=["\']([^"\']+)["\']', tag)
        mhref = re.search(r'href=["\']([^"\']+)["\']', tag)
        if not mid or not mhref:
            continue
        iid = mid.group(1)
        is_cover = iid in cover_meta or re.search(r'properties=["\'][^"\']*cover-image', tag, re.I)
        if not is_cover and cover_meta and re.search(r'media-type=["\']image/[^"\']+["\']', tag, re.I):
            is_cover = True
        if is_cover:
            remove_items[iid] = mhref.group(1)
            exact_tags.append(tag)
    if not cover_meta and not remove_items:
        return {'ok': True, 'noop': True, 'message': 'Обложка не найдена в EPUB'}
    cover_files = set()
    for iid, href in remove_items.items():
        cover_files.add(os.path.normpath(os.path.join(base, href.split('#')[0])))
    cleaned = opf
    cleaned = re.sub(r'<meta\s+[^>]*name=["\']cover["\'][^>]*/?>', '', cleaned, flags=re.I)
    for t in exact_tags:
        cleaned = cleaned.replace(t, '')
    cleaned = re.sub(r'<reference\s+[^>]*type=["\']cover["\'][^>]*/?>', '', cleaned, flags=re.I)
    for iid in remove_items:
        cleaned = re.sub(r'<itemref\s+[^>]*idref=["\']' + re.escape(iid) + r'["\'][^>]*/?>', '', cleaned, flags=re.I)

    def replacer(name, data):
        if name == opf_name:
            return cleaned.encode('utf-8')
        if os.path.normpath(name) in cover_files:
            return _EPUB_REMOVE
        return None
    try:
        backup = _backup_original(fp)
        changed = _rewrite_zip_entries(fp, replacer)
    except Exception as e:
        swallow.note('calibry_backend._epub_strip_cover_rewrite:L1594', e)
        return {'error': str(e)}
    if not changed:
        return {'error': 'Не удалось пересобрать EPUB'}
    return {'ok': True, 'backup_path': backup}


def remove_book_cover(fp) -> dict:
    """Убрать обложку из книги (fb2 — ручная чистка, остальные — ebook-meta --remove-cover)."""
    if not calibre_available():
        return {'error': 'Calibre не найден'}
    if fp.startswith('collection://'):
        def _strip(tmp):
            with open(tmp, 'rb') as f:
                raw = f.read()
            new = _fb2_strip_cover(raw)
            if new is None:
                return {'error': 'FB2 повреждён'}
            if new == raw:
                return {'ok': True, 'noop': True, 'message': 'Обложка не найдена'}
            with open(tmp, 'wb') as f:
                f.write(new)
            return {'ok': True}
        return _collection_rewrite_entry(fp, _strip)
    ext = os.path.splitext(fp)[1].lower()
    if ext == '.fb2':
        try:
            with open(fp, 'rb') as f:
                raw = f.read()
            new = _fb2_strip_cover(raw)
            if new is None:
                return {'error': 'FB2 повреждён'}
            if new == raw:
                return {'ok': True, 'noop': True, 'message': 'Обложка не найдена в книге'}
            backup = _backup_original(fp)
            tmp = fp + '.calibry_tmp'
            try:
                with open(tmp, 'wb') as f:
                    f.write(new)
                os.replace(tmp, fp)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    swallow.note('calibry_backend.remove_book_cover:L1638')
                    pass
                raise
            return {'ok': True, 'backup_path': backup}
        except Exception as e:
            swallow.note('calibry_backend.remove_book_cover:L1643', e)
            return {'error': str(e)}
    work, is_temp = _stage_input(fp)
    if ext == '.epub':
        return _epub_strip_cover_rewrite(fp)
    if ext in ('.mobi', '.azw', '.azw3', '.azw4', '.kepub', '.prc', '.pobi', '.tpz'):
        return {'error': f'Удаление обложки из "{ext}" не поддерживается Calibre 9.12. Сконвертируйте книгу в EPUB/FB2 и удалите обложку там.'}
    try:
        with _lock:
            code, out, err = _run('ebook-meta', [_posix_to_win(work), '--remove-cover'], timeout=300)
        if code != 0:
            return {'error': (err or out or '').strip()[-400:]}
        if is_temp:
            shutil.copy2(work, fp)
        return {'ok': True, 'log': (out or '').strip()[-300:]}
    except Exception as e:
        swallow.note('calibry_backend.remove_book_cover:L1659', e)
        return {'error': str(e)}
    finally:
        if is_temp and os.path.exists(work):
            try:
                os.unlink(work)
            except Exception:
                swallow.note('calibry_backend.remove_book_cover:L1666')
                pass