#!/usr/bin/env python3
"""
Web-интерфейс для просмотра JSON/JSONL файлов.
Без зависимостей — только stdlib Python.

- Список файлов → Список статей → Полная страница статьи
- Настройки чтения: шрифт, размер, межстрочный интервал, тема
- Светлая/тёмная тема
- Комментарии с вложенностью
- Поиск, пагинация

Запуск: python3 server.py --port 8080
"""

import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
import swallow
try:
    import fcntl
except ImportError:
    swallow.note('server_backup.<module>:L19')
    fcntl = None
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading

# Конфигурация
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)

# Тексты режимов ответа — общие со стримовым путём main._handle_streaming_via_asgi
import ask_modes

CFG = get_config()

# `_SERVER_DIR` нужен строкой ниже, а define-ить его должен именно монолит: у
# секции «ЛОГИРОВАНИЕ С РОТАЦИЕЙ», куда он уехал при выносе, такой же значение
# (якорь `__file__` возвращает её на этот файл), но хвостовой ре-экспорт бегает
# только в конце — голова с пустым `notes_dir` падала с NameError.
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

NOTES_DIR = CFG.get("notes_dir") or os.path.join(_SERVER_DIR, "notes")
AI_DIR = CFG.get("ai_dir", os.path.join(NOTES_DIR, "_ai_answers"))
CHAT_DIR = CFG.get("chat_dir", os.path.join(NOTES_DIR, "_chat_sessions"))
MEMORY_DIR = CFG.get("memory_dir", os.path.join(NOTES_DIR, "_memory"))
ATTACHMENTS_DIR = CFG.get("attachments_dir", os.path.join(NOTES_DIR, "_attachments"))
PLUGINS_DIR = CFG.get("plugins_dir", os.path.join(NOTES_DIR, "_plugins"))
# ===================== TEMPLATE COMMANDS (custom avatar/user commands) =====================
try:
    import template_commands as TC
    TC_AVAILABLE = True
except Exception as _e:
    TC = None
    TC_AVAILABLE = False
    logging.getLogger("infinity").warning(f"template_commands unavailable: {_e}")

# ===================== AVATAR MOTIONS (custom animations storage) =====================
try:
    import avatar_motion_storage as AMS
    AMS_AVAILABLE = True
except Exception as _e:
    AMS = None
    AMS_AVAILABLE = False
    logging.getLogger("infinity").warning(f"avatar_motion_storage unavailable: {_e}")

# ===================== AVATAR GATEWAY (queue of pending motion/text events from server to avatar) =====================
try:
    import avatar_gateway as AG
    AG_AVAILABLE = True
except Exception as _e:
    AG = None
    AG_AVAILABLE = False
    logging.getLogger("infinity").warning(f"avatar_gateway unavailable: {_e}")

# ===================== STT (faster-whisper) =====================
try:
    import stt_engine as STT
    STT_AVAILABLE = True
except Exception as _e:
    STT = None
    STT_AVAILABLE = False
    logging.getLogger("infinity").warning(f"stt_engine unavailable: {_e}")

# ===================== WAKE WORD (openWakeWord + STT fallback) =====================
try:
    import wakeword_engine as WW
    WW_AVAILABLE = True
except Exception as _e:
    WW = None
    WW_AVAILABLE = False
    logging.getLogger("infinity").warning(f"wakeword_engine unavailable: {_e}")

BOOK_EXTENSIONS = {'.epub': 'epub', '.fb2': 'fb2', '.mobi': 'mobi', '.azw3': 'azw3',
                   '.pdf': 'pdf', '.djvu': 'djvu', '.docx': 'docx', '.doc': 'doc', '.rtf': 'rtf', '.txt': 'txt', '.zip': 'zip',
                   # Форматы, читаемые через Calibre (из папки calibry/)
                   '.azw': 'azw', '.azw1': 'azw1', '.azw4': 'azw4', '.cb7': 'cb7', '.cbc': 'cbc',
                   '.cbr': 'cbr', '.cbz': 'cbz', '.chm': 'chm', '.fbz': 'fbz', '.html': 'html',
                   '.htm': 'htm', '.htmlz': 'htmlz', '.imp': 'imp', '.kepub': 'kepub', '.lit': 'lit',
                   '.lrf': 'lrf', '.lrx': 'lrx', '.odt': 'odt', '.oebzip': 'oebzip', '.opf': 'opf',
                   '.pdb': 'pdb', '.pml': 'pml', '.pmlz': 'pmlz', '.pobi': 'pobi', '.prc': 'prc',
                   '.rb': 'rb', '.snb': 'snb', '.tcr': 'tcr', '.tpz': 'tpz', '.txtz': 'txtz',
                   '.updb': 'updb', '.kepub': 'kepub',
                   # Аудиокниги: читаются встроенным плеером (books_meta)
                   '.mp3': 'mp3', '.m4b': 'm4b', '.m4a': 'm4a', '.aac': 'aac',
                   '.ogg': 'ogg', '.oga': 'oga', '.opus': 'opus', '.flac': 'flac',
                   '.wav': 'wav', '.wma': 'wma', '.aiff': 'aiff', '.aif': 'aif',
                   '.ape': 'ape', '.alac': 'alac', '.wv': 'wv', '.mka': 'mka'}

# ===================== NOTES (ПОЛЬЗОВАТЕЛЬСКИЕ СТАТЬИ) =====================

# Граф, обратные ссылки, корзина, избранное, свойства и шаблоны вынесены в
# отдельные модули: держать их в этом монолите (24k строк) было дорого в
# чтении, а модель данных у них общая — обход заметок один на всех.
# Импортируем здесь, потому что notes.py и main.py ходят в server_backup.
try:
    import notes_graph_module as _notes_graph
    import notes_model_module as _notes_model
except ImportError:  # pragma: no cover - запуск из другой директории
    swallow.note('server_backup.<module>:L117')
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import notes_graph_module as _notes_graph
    import notes_model_module as _notes_model


# === SYSTEM APPS (Linux .desktop files via xpra) — см. desktop_module.py ===
import desktop_module as DM


# ===================== СТРАНИЦА НАСТРОЕК =====================



# ===================== SHIM: PAGE_* И ХВОСТОВЫЕ РЕ-ЭКСПОРТЫ =====================
# PAGE_* живут только в файлах templates/page_*.html (app/pages_loader.py).
# До этого здесь лежали 32 HTML-литерала (~10 тысяч строк), которые фасад
# app/server при импорте всё равно перезаписывал содержимым тех же файлов.
import pages_loader as _pl

_PAGE_NAMES = (
    'PAGE_AGENT',
    'PAGE_AI_ANSWERS',
    'PAGE_APPS',
    'PAGE_APP_DETAIL',
    'PAGE_ARTICLE',
    'PAGE_ASK',
    'PAGE_AVATAR',
    'PAGE_BOOKS',
    'PAGE_HTML_LANDING',
    'PAGE_HTML_VIEWER',
    'PAGE_INDEX',
    'PAGE_INDEX_V2',
    'PAGE_LIST',
    'PAGE_MEDIA',
    'PAGE_MEMORY_BRAIN',
    'PAGE_NEWS',
    'PAGE_NOTES',
    'PAGE_NOTES_EDIT',
    'PAGE_NOTES_GRAPH',
    'PAGE_SETTINGS',
    'PAGE_SOCIAL',
    'PAGE_SOCIAL_FEED',
    'PAGE_SOCIAL_FRIENDS',
    'PAGE_SOCIAL_MESSAGES',
    'PAGE_SOCIAL_MUSIC',
    'PAGE_SOCIAL_PROFILE',
    'PAGE_SOCIAL_VIDEO',
    'PAGE_STATISTICS',
    'PAGE_TASK_MANAGER',
    'PAGE_ZIM',
    'PAGE_ZIM_VIEW',
)

for _page_name in _PAGE_NAMES:
    _page_html = _pl.read(_page_name)
    if _page_html is not None:
        globals()[_page_name] = _page_html

def __getattr__(name):
    if name.startswith('PAGE_'):
        try:
            import pages_loader as _pl2
        except Exception:
            raise AttributeError(name)
        return _pl2.get(name)
    raise AttributeError(name)

# секция 'КНИГИ' вынесена в legacy.books (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.books import (
    _book_cache,
    _books_dir_with_content,
    _calibry_read,
    _fb_extract_body_text,
    _fb_extract_text_from_element,
    _fb_process_section,
    _how_to_install,
    _read_book_file,
    _read_djvu,
    _read_doc,
    _read_docx,
    _read_epub,
    _read_fb2,
    _read_pdf,
    _read_rtf,
    _read_txt,
    _read_zip_book,
    find_books,
    get_book_content,
    search_books,
)

# секция 'ЗАКЛАДКИ' вынесена в legacy.bookmarks (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.bookmarks import (
    _BOOKMARKS_FILE,
    _add_bookmark,
    _bookmarks_lock,
    _get_bookmarks,
    _profile_bookmarks_file,
    _remove_bookmark,
)

# секция 'APP TERMINAL' вынесена в legacy.terminal (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.terminal import (
    _app_terminal_lock,
    _app_terminals,
    _start_app_terminal_windows,
    get_app_terminal_output,
    send_app_terminal_input,
    start_app_terminal,
    stop_app_terminal,
)

# секция 'ПАПКИ ЗАМЕТОК (ДРЕВОВИДНЫЕ)' вынесена в legacy.notes_folders (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.notes_folders import (
    _NOTES_CARD_CACHE,
    _NOTES_CARD_CACHE_MAX,
    _NOTES_DATA_URI_RE,
    _NOTES_IMG_PATH_CACHE,
    _NOTES_TREE_FILE,
    _load_notes_tree,
    _notes_card_image_url,
    _notes_file_by_id,
    _notes_list_entry,
    _notes_tree_lock,
    _save_notes_tree,
    notes_inline_image,
    notes_list_articles,
    notes_list_folders,
    notes_tree_create,
    notes_tree_delete,
    notes_tree_get,
    notes_tree_move,
    notes_tree_rename,
)

# секция 'ВЛОЖЕНИЯ (ATTACHMENTS)' вынесена в legacy.attachments (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.attachments import (
    ALLOWED_AUDIO_EXT,
    ALLOWED_FILE_EXT,
    ALLOWED_IMAGE_EXT,
    ALLOWED_VIDEO_EXT,
    MIME_TYPES,
    _comment_file,
    _notes_child,
    _safe_filename,
    notes_delete_attachment,
    notes_get_attachment_text,
    notes_list_all_attachments,
    notes_list_attachments,
    notes_serve_attachment,
    notes_upload_attachment,
)

# секция 'ВЕРСИИ ЗАМЕТОК' вынесена в legacy.note_versions (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.note_versions import (
    _NOTES_VERSIONS_DIR,
    notes_version_delete,
    notes_version_get,
    notes_version_list,
    notes_version_restore,
    notes_version_save,
)

# секция 'КОММЕНТАРИИ К ТЕКСТУ' вынесена в legacy.text_comments (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.text_comments import (
    _NOTES_COMMENTS_DIR,
    notes_comment_delete,
    notes_comment_list,
    notes_comment_save,
    notes_comment_update,
    notes_create_folder,
    notes_delete_folder,
    notes_move_article,
    notes_move_folder,
    notes_rename_folder,
    notes_search,
)

# секция 'ПЛАГИНЫ' вынесена в legacy.plugins (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.plugins import (
    _PY_TRUST_MARKERS,
    _plugins_cache,
    _py_plugin_trusted,
    plugins_execute_py,
    plugins_list,
    plugins_load_py,
)

# секция 'BOOK AI CHAT (РЕЖИМЫ)' вынесена в legacy.book_ai_chat (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.book_ai_chat import (
    _book_bm25_cache,
    _build_book_bm25_index,
    ask_book_question,
)

# секция 'КАТЕГОРИИ КНИГ (с AI сортировкой)' вынесена в legacy.book_categories (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.book_categories import (
    _BOOK_CATEGORIES_FILE,
    _categories_lock,
    _laya_books_enabled,
    _load_categories,
    _save_categories,
    book_categories_add_book,
    book_categories_ai_sort,
    book_categories_create,
    book_categories_delete,
    book_categories_list,
    book_categories_remove_book,
    book_categories_rename,
    book_categories_reorder,
)

# секция 'ТЕГИ И ГРАФ СВЯЗЕЙ' вынесена в legacy.tags_graph (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.tags_graph import (
    _TAGS_SYSTEM_FOLDERS,
    _notes_list_all_tags,
    _tags_cache,
    _tags_dir_sig,
    _tags_lock,
    notes_auto_links,
    notes_backlinks,
    notes_daily,
    notes_empty_trash,
    notes_from_template,
    notes_graph_data,
    notes_list_all_tags,
    notes_list_by_tag,
    notes_list_view,
    notes_outline,
    notes_patch,
    notes_properties,
    notes_restore,
    notes_search_all,
    notes_set_property,
    notes_tags_rename,
    notes_templates_delete,
    notes_templates_list,
    notes_templates_save,
    notes_trash,
    notes_unresolved_links,
)

# секция 'WORKSPACE (Рабочее пространство)' вынесена в legacy.workspace (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.workspace import (
    WORKSPACES_DIR,
    notes_delete_article,
    notes_get_article,
    notes_save_article,
    workspace_get_attachments,
    workspace_get_folders_with_notes,
    workspace_get_notes,
    workspace_get_state,
    workspace_save_state,
)

# секция 'ENTERPRISE FEATURES' вынесена в legacy.enterprise (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.enterprise import (
    _NOTES_EXPORT_DIR,
    _NOTES_TEMPLATES_FILE,
    _export_note_as_docx,
    _export_note_as_pdf,
    _extract_text_from_pdf,
    notes_export_all,
    notes_export_note,
    notes_fulltext_search,
    notes_get_templates,
    notes_import_note,
)

# секция 'ПАПКИ КНИГ (ДРЕВОВИДНЫЕ)' вынесена в legacy.book_folders (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.book_folders import (
    CHAT_SESSIONS,
    MEMORY_AVAILABLE,
    MEMORY_CATEGORIES,
    _BOOK_FOLDERS_FILE,
    _book_folders_lock,
    _chat_lock,
    _lines,
    _lines_cache,
    _load_book_folders,
    _safe_session_id,
    _save_book_folders,
    _src,
    book_folders_add_book,
    book_folders_create,
    book_folders_delete,
    book_folders_list,
    book_folders_move,
    book_folders_remove_book,
    book_folders_rename,
    chat_add_message,
    chat_create_folder,
    chat_delete_folder,
    chat_delete_message,
    chat_delete_session,
    chat_get_favorites,
    chat_get_folders,
    chat_get_messages,
    chat_get_session,
    chat_list_sessions,
    chat_move_session,
    chat_move_to_notes,
    chat_rename_folder,
    chat_rename_session,
    chat_save_folders,
    chat_toggle_favorite,
    chat_update_message,
    clear_memory,
    delete_memory,
    extract_facts_from_conversation,
    find_files,
    get_item,
    get_memory,
    get_nav,
    list_memories,
    read_page,
    save_memory,
    search,
    search_memory,
)

# секция 'AI ОТВЕТЫ (БАЗА ЗНАНИЙ)' вынесена в legacy.ai_answers (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.ai_answers import (
    _SEARCH_CACHE_TTL,
    _book_search_fallback,
    _collection_search_fallback,
    _fallback_search,
    _get_cached_search,
    _hybrid_search,
    _index_status,
    _indexing_in_progress,
    _indexing_lock,
    _run_index_background,
    _search_cache,
    _search_cache_lock,
    _set_cached_search,
    _start_background_indexing,
    _zim_search_fallback,
    ai_delete_answer,
    ai_list_answers,
    ai_save_answer,
    ai_search_answers,
    ai_update_answer,
    get_hybrid_search,
    get_index_status,
    invalidate_search_cache,
    rebuild_search_index,
    search_articles,
)

# секция 'КЭШ ШАБЛОНОВ / УТИЛИТЫ' вынесена в legacy.template_cache (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.template_cache import (
    _LLM_URL_CACHE,
    _LM_HEALTH_TTL,
    _budget_history_messages,
    _check_lm_studio_health,
    _check_lm_studio_health_uncached,
    _check_lm_studio_status,
    _clean_llm_output,
    _clean_translation_output,
    _laya_guard_context,
    _llm_endpoint_reachable,
    _lm_health_cache,
    _local_model_names,
    _parse_url_host_port,
    _template_cache,
    _template_lock,
    ask_article_question,
    get_llm_api_key,
    get_llm_base_url,
    get_llm_model,
    get_llm_url,
    lm_studio_get_models,
    lm_studio_request,
    load_template,
    resolve_llm_model,
    translate_lm_studio,
    zim_get_entry,
    zim_list_entries,
    zim_random_entry,
    zim_search,
)

# секция 'ZIM ФУНКЦИИ' вынесена в legacy.zim_funcs (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.zim_funcs import (
    _zim_cache,
    _zim_cache_lock,
    _zim_meta_cache,
    find_zim_files,
    get_zim_archive,
)

# секция 'ИСТОРИЯ ПОИСКА' вынесена в legacy.search_history (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.search_history import (
    _HISTORY_FILE,
    _add_search_history,
    _clear_search_history,
    _get_search_history,
    _history_lock,
)

# секция 'ЛОГИРОВАНИЕ С РОТАЦИЕЙ' вынесена в legacy.rotating_logging (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.rotating_logging import (
    APPS_AVAILABLE,
    APPS_DIR,
    APPS_DIRS,
    AUTH_ENABLED,
    AUTH_PASSWORD_HASH,
    AUTH_TOKEN,
    AUTH_USERNAME,
    BOOKS_AVAILABLE,
    BOOKS_DB_PATH,
    BOOKS_DIR,
    BOOKS_DIRS,
    CHAT_FOLDERS_FILE,
    CUSTOM_API_KEY,
    CUSTOM_BASE_URL,
    CUSTOM_DIMENSION,
    CUSTOM_EMBEDDING_DB,
    CUSTOM_FAISS_PATH,
    CUSTOM_INDEX_DIR,
    CUSTOM_MODEL,
    CUSTOM_URL,
    DATA_DIRS,
    DEFAULT_APPS_DIR,
    EXTENSIONS_DIR,
    HERMES_MOUNT,
    HTML_AVAILABLE,
    HTML_DIR,
    HTML_DIRS,
    LEGACY_ASIO_ENABLED,
    LLAMA_CPP_API_KEY,
    LLAMA_CPP_BASE_URL,
    LLAMA_CPP_MODEL,
    LLAMA_CPP_URL,
    LLM_BACKEND,
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
    LM_STUDIO_EMBED_MODEL,
    LM_STUDIO_MODEL,
    LM_STUDIO_TIMEOUT,
    LM_STUDIO_URL,
    MEDIA_AVAILABLE,
    MEDIA_CFG,
    MUSIC_DIRS,
    NEWS_AVAILABLE,
    PHOTOS_DIR,
    PHOTO_AVAILABLE,
    PHOTO_DIRS,
    PIPER_TTS_AVAILABLE,
    PIPER_VOICE,
    QWEN3_CLIENT,
    QWEN3_DEFAULT_LANGUAGE,
    QWEN3_DEFAULT_SPEAKER,
    QWEN3_MODEL_SIZE,
    QWEN3_SPEAKERS,
    QWEN3_TTS_AVAILABLE,
    QWEN3_URL,
    SILERO_MODEL,
    SILERO_MODEL_VERSION,
    SILERO_SAMPLE_RATE,
    SILERO_TTS_AVAILABLE,
    TTS_ENGINE,
    VIDEO_DIRS,
    WIKI_MEMORY_DIR,
    ZIM_AVAILABLE,
    ZIM_DIRS,
    _APBACK_DIR,
    _AUTH_CFG,
    _CUSTOM_CFG,
    _DJVU_BACKEND,
    _INDEX_CFG,
    _LLAMA_CPP_CFG,
    _LM_CFG,
    _LOG_CFG,
    _NEWS_SCHED_LOCK,
    _NEWS_SCHED_STARTED,
    _PIPER_FAILED,
    _PIPER_LOADED_PATH,
    _PIPER_VOICE_CACHE,
    _PIPER_VOICE_CACHE_LOCK,
    _PROJECT_APPS_DIR,
    _SEARCH_CFG,
    _SILERO_HUB_DIR,
    _SILERO_POOL,
    _SILERO_TORCH_HUB_DIR,
    _TTS_INIT_LOCK,
    _TTS_INIT_STARTED,
    _TTS_INIT_THREAD,
    _TTS_JUNK_RE,
    _TTS_READY_EVENT,
    _TTS_WARMUP_DELAY,
    _apply_silero_rate_pitch,
    _apply_silero_rate_pitch_to_wav,
    _console_handler,
    _ensure_news_schedulers,
    _ensure_tts_ready,
    _file_handler,
    _find_piper_model,
    _get_piper_cache_dir,
    _get_piper_model_dir,
    _get_piper_voice,
    _get_silero_hub_dir,
    _init_qwen3,
    _init_tts_background,
    _list_piper_voices,
    _list_silero_speakers,
    _load_silero_v5,
    _log_backup_count,
    _log_file,
    _log_level,
    _log_max_bytes,
    _public_auth_cfg,
    _publish_tts_state,
    _tts_publish,
    _qwen3_port_open,
    _qwen3_synthesize,
    _sanitize_tts_text,
    _start_news_schedulers_later,
    _start_tts_init_async,
    _warmup_tts_later,
    _wikimemory_dir,
    logger,
    shutil,
)
# имена, которые секция связывает только внутри try/if: переносятся, только
# если связались (иначе монолит их не имел и раньше).
import legacy.rotating_logging as _legacy_rotating_logging
for _n in ('ARTISTS_DIR', 'Archive', 'BACKDROPS_DIR', 'BeautifulSoup', 'COVERS_DIR', 'CROSS_ENCODER_AVAILABLE', 'EmbeddingCache', 'Entry', 'FAISS_AVAILABLE', 'FAVORITES_FILE', 'FaissIndex', 'HybridSearch', 'INDEX_DIR', 'INDEX_PROGRESS', 'LYRICS_DIR', 'MEDIA_DIR', 'MM', 'MOVIE_CACHE_FILE', 'MUSIC_INDEX_FILE', 'NLTK_AVAILABLE', 'PAGE_APPS', 'PAGE_HTML_LANDING', 'PAGE_HTML_VIEWER', 'PAGE_MEDIA', 'PAGE_NEWS', 'PHOTO', 'PLAYLISTS_DIR', 'POSTERS_DIR', 'PYMORPHY_AVAILABLE', 'PiperVoice', 'PyPDF2', 'QDRANT_AVAILABLE', 'Query', 'SEARCH_ENGINE_AVAILABLE', 'SEARCH_ENGINE_VERSION', 'SYMSPELL_AVAILABLE', 'SearchIndex', 'Searcher', 'TANTIVY_AVAILABLE', 'VIDEO_INDEX_FILE', 'WHOOSH_AVAILABLE', '_LM_EMBED_AVAILABLE', '_LM_RERANKER_AVAILABLE', '_LM_RERANKER_MODEL_ID', '_PIPER_CONFIG', '_PIPER_MODEL', '_PIPER_SOURCE', '_compute_recency_boost', '_d', '_photo_cfg_dirs', '_preprocess_fuzzy_query', '_save_json', '_save_track_cover', 'add_rss_source', 'add_to_collection_direct', 'add_to_favorites', 'add_track_to_playlist', 'check_video_file_health', 'clear_music_index', 'clear_video_index', 'collect_documents', 'convert_avi_videos', 'correct_spelling', 'create_playlist', 'delete_collection_direct', 'delete_playlist', 'delete_track_server', 'detect_changes', 'detect_intent', 'discover_rss_from_url', 'docx', 'ebooklib', 'epub', 'etree', 'expand_query', 'expand_query_with_synonyms', 'extract_best_passages', 'extract_html_meta', 'extract_html_text', 'fetch_all_feeds', 'fetch_feed_articles', 'fetch_lyrics_from_internet', 'fetch_movie_metadata', 'find_apps', 'find_duplicates', 'find_html_files', 'find_random_video', 'format_search_results', 'get_apps_cached', 'get_article', 'get_articles', 'get_artist_albums', 'get_artist_info', 'get_artist_tracks', 'get_categories', 'get_favorites', 'get_html_files_cached', 'get_intent_boost', 'get_lyrics', 'get_media_statistics', 'get_movie_metadata_cached', 'get_music_index', 'get_playlist', 'get_playlists', 'get_recommendations', 'get_rss_sources', 'get_running_servers', 'get_track_filepath', 'get_video_index', 'group_tracks_by_album', 'group_tracks_by_artist', 'group_tracks_by_genre', 'incremental_rebuild', 'is_favorite', 'lemmatize_word', 'load_app_json', 'log_play_server', 'log_watch_server', 'news_module', 'rebuild_music_index', 'rebuild_video_index', 'remove_from_collection_direct', 'remove_from_favorites', 'remove_rss_source', 'remove_track_from_playlist', 'rerank_with_cross_encoder', 'save_artist_info', 'save_lyrics', 'save_series_server', 'scan_music_directories', 'scan_video_directories', 'search_html_files', 'search_rss_feeds', 'search_tracks', 'set_db_path', 'set_downloads_dir', 'share_app', 'start_auto_refresh', 'start_digest_scheduler', 'start_transcode_server', 'stem_text', 'sync_media_dirs_from_config', 'toggle_source', 'update_playlist', ):
    if hasattr(_legacy_rotating_logging, _n):
        globals()[_n] = getattr(_legacy_rotating_logging, _n)

# секция 'APP SERVER MANAGEMENT' вынесена в legacy.apps_management (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.apps_management import (
    _EXT_RUNTIME_JS,
    _PROFILE_FLIP_LOCK,
    _app_server_lock,
    _app_servers,
    apply_profile_globals,
    check_idle_servers,
    get_app_server_status,
    run_rebuild_selected,
    start_app_server,
    stop_app_server,
)

# секция 'СТРАНИЦЫ' вынесена в legacy.pages_data (tools/_extract_section.py).
# Явный список имён, а не `import *`: приватные имена тоже должны
# остаться в этом модуле — на них ссылаются 62 файла через `import server`.
from legacy.pages_data import (
    _EXT_LOADER_CACHE,
    _piper_download,
    _piper_install_dir,
    _tts_state,
    build_extension_loader,
    handle_install_piper,
    handle_install_silero,
    handle_tts_voices,
)

# ---- связывание чужих имён во вынесенных модулях ----
# Каждый вынесенный модуль перечисляет в _FOREIGN имена из других секций.
# Связываем их здесь, после ВСЕХ ре-экспортов: тогда неважно, в каком порядке
# секции были вынесены, и частично инициализированный модуль не разбирается.
_unbound = []
for _legacy_mod in [v for _k, v in list(sys.modules.items())
                    if str(_k).startswith('legacy.') and hasattr(v, '_bind')]:
    _unbound += [_legacy_mod.__name__ + '.' + _n
                 for _n in _legacy_mod._bind(globals())]
if _unbound:
    _msg = 'не связаны чужие имена: %s' % _unbound
    if os.environ.get('INFINITY_STRICT_BIND') == '1':
        raise RuntimeError(_msg)
    logging.getLogger('infinity').warning(
        '%s — старт продолжается без этих имён '
        '(INFINITY_STRICT_BIND=1 включает строгую проверку)', _msg)
