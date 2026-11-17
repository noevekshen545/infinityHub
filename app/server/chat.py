#!/usr/bin/env python3
"""
Чат-сессии и сообщения.
Ре-экспортирует функции из server_backup.
"""

import logging
logger = logging.getLogger("infinity")

import server_backup as _original

# Ре-экспорт функций чата
_CHAT_FUNCTIONS = [
    'chat_list_sessions', 'chat_get_session', 'chat_get_messages',
    'chat_add_message', 'chat_delete_session', 'chat_delete_all_sessions',
    'chat_get_folders', 'chat_create_session',
]

for _name in _CHAT_FUNCTIONS:
    if hasattr(_original, _name):
        globals()[_name] = getattr(_original, _name)

# Ре-экспорт данных чата
_CHAT_DATA = [
    'CHAT_SESSIONS', '_chat_lock', 'CHAT_DIR',
]

for _name in _CHAT_DATA:
    if hasattr(_original, _name):
        globals()[_name] = getattr(_original, _name)
