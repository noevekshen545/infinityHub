#!/usr/bin/env python3
"""
path_compat — единая нормализация путей для переносимой сборки.

Задача модуля: в сборке один и тот же config.json переносится между
Windows и WSL, и раньше пути, сохранённые из UI, тупо превращались в
/mnt/e/... даже на чистом Windows, где такой путь не существует. Из-за
этого «Книги», «ZIM» и загрузка сайтов не находили свои каталоги.

Правила:
  * на Windows: /mnt/<буква>/... → <БУКВА>:\\...,
                //wsl.localhost/<дистр>/... и //wsl$/... → UNC,
                C:\\... остаётся как есть;
  * в WSL:  C:\\... и E:/... → /mnt/<буква>/...;
  * нативные Linux-пути (/home/..., /usr/...) не трогаем никогда —
    на Windows их приводить не к чему.
"""

import os
import re
import sys
import swallow

_WIN_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/](.*)$")
_MNT_RE = re.compile(r"^/mnt/([a-zA-Z])/(.*)$")
_MNT_ROOT_RE = re.compile(r"^/mnt/([a-zA-Z])$")
_WSL_UNC_RE = re.compile(r"^//wsl(?:\.localhost|\$)/([^/]+)(/.*)?$", re.IGNORECASE)


def is_windows() -> bool:
    return sys.platform == "win32"


def is_wsl() -> bool:
    """True, если Python запущен внутри WSL (а не на чистом Linux/Windows)."""
    if is_windows():
        return False
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        swallow.note('path_compat.is_wsl:L41')
        return False


def posix_to_win(p: str) -> str:
    """/mnt/c/foo → C:\\foo ; //wsl.localhost/Ubuntu/home → \\\\wsl.localhost\\Ubuntu\\home."""
    if not p or not isinstance(p, str):
        return p
    s = p.replace("\\", "/")
    m = _WSL_UNC_RE.match(s)
    if m:
        dist = m.group(1)
        rest = (m.group(2) or "").lstrip("/")
        tail = ("\\" + rest.replace("/", "\\")) if rest else ""
        return f"\\\\wsl.localhost\\{dist}{tail}"
    m = _MNT_RE.match(s)
    if m:
        rest = m.group(2).replace("/", "\\")
        return f"{m.group(1).upper()}:\\{rest}"
    m = _MNT_ROOT_RE.match(s)
    if m:
        return f"{m.group(1).upper()}:\\"
    return p


def win_to_posix(p: str) -> str:
    """C:\\foo → /mnt/c/foo ; \\\\wsl.localhost\\Ubuntu\\home → /home (внутри WSL)."""
    if not p or not isinstance(p, str):
        return p
    s = p.replace("\\", "/")
    m = _WSL_UNC_RE.match(s)
    if m:
        rest = (m.group(2) or "").lstrip("/")
        return "/" + rest if rest else "/"
    m = _WIN_DRIVE_RE.match(s)
    if m:
        rest = m.group(2)
        return f"/mnt/{m.group(1).lower()}/{rest}" if rest else f"/mnt/{m.group(1).lower()}"
    return p


def to_native(p: str) -> str:
    """Привести путь к формату текущей ОС (там, где это вообще возможно)."""
    if not p or not isinstance(p, str):
        return p
    if is_wsl():
        return win_to_posix(p)
    if is_windows():
        return posix_to_win(p)
    return p


def looks_like_path(v) -> bool:
    return isinstance(v, str) and bool(v) and (
        ("/" in v) or ("\\" in v) or v.startswith("~") or "${" in v
    )


def normalize_config_paths(obj):
    """Рекурсивно привести все строковые пути в конфиге к нативному виду."""
    if isinstance(obj, dict):
        return {k: normalize_config_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_config_paths(i) for i in obj]
    if looks_like_path(obj):
        return to_native(obj)
    return obj
