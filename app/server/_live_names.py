#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Список имён, которые пакет `server` НЕ должен копировать по значению.

`app/server/__init__.py` копирует атрибуты `server_backup` в свой словарь при
импорте. Для констант и контейнеров (dict/set/список) это безопасно: объект один.
Для имён, которые ПОСЛЕ импорта переприсваивают через `global X` (в функциях
монолита или любой вынесенной секции `app/legacy/*.py`), копия устаревает
навсегда — ровно тот класс поломки, из-за которого `/api/tts/voices` отвечал
`available:false` при загруженной модели. Для имён из этого списка фасад читает
оригинал живьём.

Не правь руками: перегенерируй `PYTHONIOENCODING=utf-8 python tools/_name_split_gate.py --emit`.
"""
LIVE_NAMES = frozenset({
    "PIPER_TTS_AVAILABLE",
    "PIPER_VOICE",
    "QWEN3_CLIENT",
    "QWEN3_TTS_AVAILABLE",
    "SILERO_MODEL",
    "SILERO_MODEL_VERSION",
    "SILERO_TTS_AVAILABLE",
    "TTS_ENGINE",
    "_EXT_LOADER_CACHE",
    "_EXT_RUNTIME_JS",
    "_LLM_URL_CACHE",
    "_NEWS_SCHED_STARTED",
    "_PIPER_LOADED_PATH",
    "_SILERO_POOL",
    "_TTS_INIT_STARTED",
    "_TTS_INIT_THREAD",
    "_app_servers",
    "_app_terminals",
    "_hybrid_search",
    "_index_status",
    "_indexing_in_progress",
    "_lines_cache",
    "_plugins_cache",
})
