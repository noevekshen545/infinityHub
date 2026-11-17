#!/usr/bin/env python3
"""
Модуль конфигурации. Обёртка над CFG из server_backup.
Вынесен для удобства и избежания прямых зависимостей от server_backup.
"""

import os, sys, json, logging
from typing import Optional, Dict, Any

logger = logging.getLogger("infinity")

# Импортируем оригинальный CFG из server_backup
import server_backup as _original
CFG = getattr(_original, 'CFG', {})
_orig_get_config = getattr(_original, 'get_config', None)
_orig_load_config = getattr(_original, 'load_config', None)


def get(key: str, default=None) -> Any:
    """Безопасное получение значения из конфига по ключу (поддерживает точечную нотацию)."""
    keys = key.split('.')
    val = CFG
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, {})
        else:
            return default
    return val if val != {} else default


def get_server_port() -> int:
    """Порт HTTP-сервера."""
    return get('server.port', 8080)


def get_data_dirs() -> list:
    """Директории с данными."""
    return get('data_dirs', [])


def get_zim_dirs() -> list:
    """Директории ZIM-архивов."""
    return get('zim_dirs', [])


def get_books_dir() -> str:
    """Директория книг."""
    return get('books_dir', os.path.expanduser('~/books'))


def get_calibry_dir() -> str:
    """Директория портабельного Calibre (calibry_dir)."""
    return get('calibry_dir', '')


def get_notes_dir() -> str:
    """Директория заметок."""
    return get('notes_dir', os.path.expanduser('~/notes'))


def get_chat_dir() -> str:
    """Директория чатов."""
    return get('chat_dir', os.path.join(get_notes_dir(), '_chat_sessions'))


def get_ai_dir() -> str:
    """Директория AI-ответов."""
    return get('ai_dir', os.path.join(get_notes_dir(), '_ai_answers'))


def get_memory_dir() -> str:
    """Директория памяти AI."""
    return get('memory_dir', os.path.join(get_notes_dir(), '_memory'))


def reload() -> Dict[str, Any]:
    """Перезагрузить конфиг из файла."""
    global CFG
    if _orig_load_config:
        CFG = _orig_load_config()
    return CFG
