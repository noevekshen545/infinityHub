#!/usr/bin/env python3
"""
binaries_env — портативные бинарники комплекта в PATH.

В сборке рядом с кодом лежат ffmpeg, VLC, Tor и другие внешние
программы (app/binaries/...). Раньше они не попадали в PATH, и
ffmpeg/pydub/субтитры/транскодер падали с «ffmpeg не найден», хотя
файлы лежали на диске.

Модуль добавляет в PATH (в начало) каталоги всех найденных бинарников
и выставляет VLC_PLUGIN_PATH / PYTHON_VLC_LIB_PATH для python-vlc.
Вызывается один раз при старте сервера.
"""

import os
import sys
import swallow

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _add(path):
    if not path or not os.path.isdir(path):
        return False
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep) if cur else []
    if path in parts:
        return False
    os.environ["PATH"] = path + os.pathsep + cur
    return True


def ffmpeg_bin_dir():
    """Каталог с ffmpeg/ffprobe: сначала распакованный, потом исходный zip."""
    base = os.path.join(_APP_DIR, "binaries", "ffmpeg")
    if not os.path.isdir(base):
        return None
    direct = os.path.join(base, "bin")
    if os.path.isfile(os.path.join(direct, "ffmpeg.exe")) or \
            os.path.isfile(os.path.join(direct, "ffmpeg")):
        return direct
    try:
        for name in sorted(os.listdir(base)):
            cand = os.path.join(base, name, "bin")
            if os.path.isdir(cand) and (
                    os.path.isfile(os.path.join(cand, "ffmpeg.exe")) or
                    os.path.isfile(os.path.join(cand, "ffmpeg"))):
                return cand
    except OSError:
        swallow.note('binaries_env.ffmpeg_bin_dir:L49')
        pass
    return None


def vlc_dir():
    """Каталог portable-VLC (тот, где лежат vlc.exe и libvlc.dll)."""
    base = os.path.join(_APP_DIR, "vlc", "portable")
    if not os.path.isdir(base):
        return None
    if os.path.isfile(os.path.join(base, "vlc.exe")):
        return base
    try:
        for name in sorted(os.listdir(base)):
            cand = os.path.join(base, name)
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "vlc.exe")):
                return cand
    except OSError:
        swallow.note('binaries_env.vlc_dir:L67')
        pass
    return None


def tor_dir():
    cand = os.path.join(_APP_DIR, "binaries", "tor")
    return cand if os.path.isdir(cand) else None


def setup_binaries_env(verbose=False):
    """Добавить портативные бинарники в PATH. Возвращает список добавленных."""
    added = []
    ff = ffmpeg_bin_dir()
    if ff and _add(ff):
        added.append(ff)
    vl = vlc_dir()
    if vl:
        if _add(vl):
            added.append(vl)
        # python-vlc: PYTHON_VLC_LIB_PATH — полный путь к libvlc.dll,
        # PYTHON_VLC_MODULE_PATH — каталог плагинов. Без них import vlc
        # падал с «Could not find module libvlc.dll».
        lib = os.path.join(vl, "libvlc.dll")
        if os.path.isfile(lib):
            os.environ["PYTHON_VLC_LIB_PATH"] = lib
        plugins = os.path.join(vl, "plugins")
        if os.path.isdir(plugins):
            os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
            os.environ.setdefault("VLC_PLUGIN_PATH", plugins)
    tr = tor_dir()
    if tr and _add(tr):
        added.append(tr)
    if verbose and added:
        print("[binaries] PATH += " + os.pathsep.join(added))
    return added


def configure_vlc_module():
    """Научить python-vlc использовать portable-VLC (иначе libvlc.dll не найден)."""
    vl = vlc_dir()
    if not vl:
        return False
    lib = os.path.join(vl, "libvlc.dll")
    if os.path.isfile(lib):
        os.environ["PYTHON_VLC_LIB_PATH"] = lib
    plugins = os.path.join(vl, "plugins")
    if os.path.isdir(plugins):
        os.environ["PYTHON_VLC_MODULE_PATH"] = plugins
        os.environ.setdefault("VLC_PLUGIN_PATH", plugins)
    return True


if __name__ == "__main__":
    for p in setup_binaries_env(verbose=True):
        print("added:", p)
    print("ffmpeg:", shutil_which := __import__("shutil").which("ffmpeg"))
