#!/usr/bin/env python3
"""
Modular Infinity AI Search server package.

Ре-экспортирует всё из server_backup.py для обратной совместимости.
Постепенно код будет переноситься в подмодули этого пакета.
"""

import sys, os

# Сначала импортируем подмодули, пока у нас чистый неймспейс
from . import config as _config_mod
from . import templates as _templates_mod
from . import search as _search_mod
from . import chat as _chat_mod
from . import tts as _tts_mod
from . import media as _media_mod
from . import notes as _notes_mod
from . import books as _books_mod
from . import social as _social_mod
from . import education as _education_mod
from . import auth as _auth_mod
from . import torrent as _torrent_mod

# Сохраняем подмодули под особыми именами, чтобы server_backup их не перезатёр
_dot_modules = {
    'config': _config_mod,
    'templates': _templates_mod,
    'search': _search_mod,
    'chat': _chat_mod,
    'tts': _tts_mod,
    'media': _media_mod,
    'notes': _notes_mod,
    'books': _books_mod,
    'social': _social_mod,
    'education': _education_mod,
    'torrent': _torrent_mod,
    'auth': _auth_mod,
}

# Теперь импортируем server_backup и копируем всё в текущий модуль
import server_backup as _original
import swallow

# Кроме имён, которые переприсваиваются ПОСЛЕ импорта: копия по значению для них
# = вечная ложь (именно так `/api/tts/voices` года мог отвечать available:false при
# загруженной модели). Список порождает tools/_name_split_gate.py --emit.
try:
    from ._live_names import LIVE_NAMES as _live_names
except Exception:
    swallow.note('__init__.<module>:L50')
    _live_names = frozenset()

_exclude = frozenset({
    '__builtins__', '__cached__', '__loader__', '__package__', '__spec__',
    '__name__', '__file__', '__doc__',
})

for _attr_name in dir(_original):
    if _attr_name in _exclude:
        continue
    if _attr_name.startswith('__') and _attr_name.endswith('__'):
        continue
    if _attr_name in _live_names:
        # не замораживаем: читается через __getattr__ живьём из _original
        continue
    try:
        globals()[_attr_name] = getattr(_original, _attr_name)
    except Exception:
        swallow.note('__init__.<module>:L69')
        pass


def __getattr__(name):
    """Отдать из `server_backup` то, что фасад намеренно не копирует.

    Вызывается только для имён, которых нет в словаре пакета, — то есть для `_live_names`
    и для всего, что появится в монолите позже импорта фасада.
    """
    try:
        return getattr(_original, name)
    except AttributeError:
        raise AttributeError("module 'server' has no attribute %r" % name) from None


def __dir__():
    return sorted(set(globals()) | set(_live_names) | set(dir(_original)))

# Восстанавливаем подмодули (на случай, если server_backup перезатёр их)
for _name, _mod in _dot_modules.items():
    globals()[_name] = _mod

# Подмодули не должны перезатирать одноимённые функции из server_backup
# (например, server.search — это функция search() из server_backup,
# а не модуль server/search.py).
for _name, _mod in _dot_modules.items():
    _orig_attr = getattr(_original, _name, None)
    if callable(_orig_attr):
        globals()[_name] = _orig_attr

# Загружаем PAGE_* из templates/*.html (перезаписывает хардкод из server_backup)
try:
    from . import templates_static as _ts
    for _ts_name in dir(_ts):
        if _ts_name.startswith('PAGE_') and not _ts_name.endswith('_LOCK') and not _ts_name.endswith('_CACHE'):
            _ts_val = getattr(_ts, _ts_name, None)
            if _ts_val and isinstance(_ts_val, str) and len(_ts_val) > 100:
                globals()[_ts_name] = _ts_val
                # Часть legacy-хелперов в server/*.py читает _original.PAGE_*,
                # поэтому дублируем переопределённые шаблоны и в server_backup.
                try:
                    setattr(_original, _ts_name, _ts_val)
                except Exception as _e2:
                    logger.warning(f"[templates_static] setattr({_ts_name}) on _original failed: {_e2}")
    del _ts, _ts_name, _ts_val
except Exception as _e:
    logger.warning(f"[templates_static] import/override loop failed: {_e}")

# ============================================================
# HTTP Pool: подменяем lm_studio_request на версию с HTTP-пулом
# ============================================================
try:
    from worker_pool import HTTP_POOL

    def _lm_studio_request_pooled(messages, max_tokens=2048, model=None, temperature=0.7, stream=False, context_length=None, vision_image_base64=None, enable_thinking=None):
        """lm_studio_request с переиспользованием HTTP-соединений."""
        m = _original.resolve_llm_model(model)

        if vision_image_base64 and messages:
            import base64 as _b64
            try:
                img_data = _b64.b64decode(vision_image_base64)
                mime_type = "image/jpeg"
                if img_data[:4] == b'\x89PNG':
                    mime_type = "image/png"
                elif img_data[:4] == b'RIFF' and img_data[8:12] == b'WEBP':
                    mime_type = "image/webp"
                data_url = f"data:{mime_type};base64,{vision_image_base64}"
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        messages[i]["content"] = [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": messages[i].get("content", "")}
                        ]
                        break
            except Exception:
                swallow.note('__init__._lm_studio_request_pooled:L146')
                pass

        payload_dict = {
            "model": m,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if _original.LLM_BACKEND == "llamacpp":
            _thinking = False if enable_thinking is None else bool(enable_thinking)
            payload_dict["chat_template_kwargs"] = {"enable_thinking": _thinking}
        elif not enable_thinking and "qwen" in m.lower():
            payload_dict["reasoning"] = {"enabled": False}
        if context_length and _original.LLM_BACKEND != "llamacpp":
            payload_dict["context_length"] = context_length

        headers = {"Content-Type": "application/json"}
        _api_key = _original.get_llm_api_key()
        if _api_key:
            headers["Authorization"] = f"Bearer {_api_key}"

        try:
            status, data = HTTP_POOL.request_json(
                "POST", _original.get_llm_url(),
                headers=headers, json_data=payload_dict,
                timeout=600
            )
            if status != 200:
                err_msg = data.get("error", {}).get("message", "") or data.get("error", "") or data.get("detail", "") or str(data)
                _original.logger.error(f"LM Studio HTTP {status}: {err_msg}")
                return {"content": f"Ошибка AI ({status}): {err_msg}", "reasoning": ""}
            if "choices" not in data or not data["choices"]:
                err_msg = data.get("error", {}).get("message", "") or data.get("error", "") or data.get("detail", "") or "пустой ответ"
                _original.logger.error(f"LM Studio no choices: {err_msg}")
                return {"content": f"Ошибка AI: {err_msg}", "reasoning": ""}
            choice = data["choices"][0]["message"]
            return {
                "content": choice.get("content", ""),
                "reasoning": choice.get("reasoning_content", ""),
            }
        except Exception as e:
            _original.logger.error(f"LM Studio error (pooled): {e}")
            return {"content": f"Ошибка: {e}", "reasoning": ""}

    # Подменяем в server_backup: legacy-хелперы доменных модулей зовут
    # _original.lm_studio_request, они тоже должны ходить через пул.
    setattr(_original, 'lm_studio_request', _lm_studio_request_pooled)
    globals()['lm_studio_request'] = _lm_studio_request_pooled
    _original.logger.info("[HTTP Pool] lm_studio_request переведён на HTTP-пул соединений")
except Exception as _e:
    _original.logger.error(f"[HTTP Pool] не удалось настроить пул: {_e}")

# ============================================================
# Async HTTP: асинхронная версия lm_studio_request для FastAPI роутов
# ============================================================
async def async_lm_studio_request(messages, max_tokens=2048, model=None, temperature=0.7,
                                    stream=False, context_length=None, vision_image_base64=None, enable_thinking=None):
    """Асинхронный lm_studio_request через httpx (не блокирует thread pool)."""
    from worker_pool import async_request_json
    m = _original.resolve_llm_model(model)
    payload_dict = {"model": m, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": stream}
    if _original.LLM_BACKEND == "llamacpp":
        _thinking = False if enable_thinking is None else bool(enable_thinking)
        payload_dict["chat_template_kwargs"] = {"enable_thinking": _thinking}
    elif not enable_thinking and "qwen" in m.lower():
        payload_dict["reasoning"] = {"enabled": False}
    if context_length and _original.LLM_BACKEND != "llamacpp":
        payload_dict["context_length"] = context_length
    headers = {"Content-Type": "application/json"}
    _api_key = _original.get_llm_api_key()
    if _api_key:
        headers["Authorization"] = f"Bearer {_api_key}"
    try:
        status, data = await async_request_json("POST", _original.get_llm_url(), headers=headers, json_data=payload_dict, timeout=600)
        if status != 200:
            err_msg = data.get("error", {}).get("message", "") or data.get("error", "") or data.get("detail", "") or str(data)
            _original.logger.error(f"LM Studio async HTTP {status}: {err_msg}")
            return {"content": f"Ошибка AI ({status}): {err_msg}", "reasoning": ""}
        if "choices" not in data or not data["choices"]:
            err_msg = data.get("error", {}).get("message", "") or data.get("error", "") or data.get("detail", "") or "пустой ответ"
            return {"content": f"Ошибка AI: {err_msg}", "reasoning": ""}
        choice = data["choices"][0]["message"]
        return {"content": choice.get("content", ""), "reasoning": choice.get("reasoning_content", "")}
    except Exception as e:
        _original.logger.error(f"LM Studio async error: {e}")
        return {"content": f"Ошибка: {e}", "reasoning": ""}

# ---------------------------------------------------------------
# Нативные реализации, перенесённые из монолита (миграция P6).
# Обязаны идти после копирования dir(_original) и после цикла callable-
# приоритета: иначе перенесённая функция была бы перезатёрта легаси-версией.
# Тела совпадают с оригиналом по AST (проверка — E:/tmp/port_parity.py), поэтому
# переопределение меняет только адрес кода, не поведение. Сбой импорта не должен
# ронять пакет: остаётся легаси-версия, а лог говорит, что именно не поднялось.
# ---------------------------------------------------------------
def _apply_native_ports():
    import importlib
    _ports = (
        ("modules.zim_store", ("get_zim_archive", "zim_get_entry", "zim_list_entries",
                              "zim_search", "zim_random_entry")),
        ("modules.notes_workspace", ("workspace_get_state", "workspace_save_state",
                                     "workspace_get_notes", "workspace_get_attachments",
                                     "workspace_get_folders_with_notes")),
        ("modules.books_store", ("book_categories_list", "book_categories_create",
                                 "book_categories_rename", "book_categories_delete",
                                 "book_categories_add_book", "book_categories_remove_book",
                                 "book_categories_reorder", "book_categories_ai_sort",
                                 "book_folders_list", "book_folders_create",
                                 "book_folders_rename", "book_folders_move",
                                 "book_folders_delete", "book_folders_add_book",
                                 "book_folders_remove_book")),
        ("modules.chat_store", ("chat_get_folders", "chat_save_folders",
                                "chat_create_folder", "chat_rename_folder",
                                "chat_delete_folder", "chat_get_session",
                                "chat_add_message", "chat_update_message",
                                "chat_delete_message", "chat_toggle_favorite",
                                "chat_get_favorites", "chat_move_to_notes",
                                "chat_get_messages", "chat_list_sessions",
                                "chat_delete_session", "chat_rename_session",
                                "chat_move_session")),
        ("modules.ai_answers", ("ai_save_answer", "ai_update_answer", "ai_delete_answer",
                                "ai_search_answers", "ai_list_answers")),
        ("modules.notes_store", ("notes_auto_links", "notes_backlinks",
                                 "notes_comment_delete", "notes_comment_list",
                                 "notes_comment_save", "notes_comment_update",
                                 "notes_daily", "notes_delete_attachment",
                                 "notes_get_article", "notes_graph_data",
                                 "notes_list_all_attachments", "notes_list_all_tags",
                                 "notes_list_articles", "notes_list_attachments",
                                 "notes_list_by_tag", "notes_list_folders",
                                 "notes_list_view", "notes_move_folder", "notes_outline",
                                 "notes_properties", "notes_rename_folder",
                                 "notes_save_article", "notes_search", "notes_search_all",
                                 "notes_templates_list", "notes_tree_create",
                                 "notes_tree_delete", "notes_tree_get", "notes_tree_move",
                                 "notes_tree_rename", "notes_unresolved_links",
                                 "notes_version_delete", "notes_version_get",
                                 "notes_version_list", "notes_version_restore",
                                 "notes_version_save")),
        ("modules.store_files", ("_notes_child", "_safe_filename", "_safe_session_id",
                                 "_comment_file", "_load_notes_tree", "_save_notes_tree",
                                 "_tags_dir_sig", "_load_categories", "_save_categories",
                                 "_load_book_folders", "_save_book_folders")),
        ("modules.llm_service", ("get_llm_api_key", "get_llm_url", "get_llm_model",
                                 "resolve_llm_model", "_llm_endpoint_reachable",
                                 "_local_model_names", "lm_studio_request")),
        ("modules.notes_crud", ("_notes_card_image_url", "_notes_file_by_id",
                                 "_notes_list_all_tags", "_notes_list_entry",
                                 "notes_create_folder", "notes_delete_article",
                                 "notes_delete_folder", "notes_empty_trash",
                                 "notes_export_all", "notes_export_note",
                                 "notes_from_template", "notes_fulltext_search",
                                 "notes_get_attachment_text", "notes_get_templates",
                                 "notes_import_note", "notes_inline_image",
                                 "notes_move_article", "notes_patch", "notes_restore",
                                 "notes_serve_attachment", "notes_set_property",
                                 "notes_tags_rename", "notes_templates_delete",
                                 "notes_templates_save", "notes_trash",
                                 "notes_upload_attachment")),
        ("modules.books_read", ("_read_txt", "_read_epub", "_read_fb2",
                                 "_fb_extract_text_from_element",
                                 "_fb_extract_body_text", "_fb_process_section",
                                 "_how_to_install", "_read_pdf", "_read_docx",
                                 "_read_djvu", "_read_rtf", "_read_doc",
                                 "_calibry_read", "_read_book_file",
                                 "_read_zip_book", "_books_dir_with_content",
                                 "find_books", "get_book_content", "search_books",
                                 "read_page", "get_item", "ask_book_question",
                                 "get_nav", "_build_book_bm25_index")),
        ("modules.search_store", ("_get_search_history", "_add_search_history",
                                  "_clear_search_history", "_profile_bookmarks_file",
                                  "_get_bookmarks", "_add_bookmark",
                                  "_remove_bookmark")),
        ("modules.llm_text", ("_clean_llm_output", "_clean_translation_output",
                              "_sanitize_tts_text", "get_llm_base_url",
                              "lm_studio_get_models", "_parse_url_host_port",
                              "_check_lm_studio_health_uncached",
                              "_check_lm_studio_health", "translate_lm_studio")),
        ("modules.assets_index", ("find_zim_files", "find_files", "_src", "_lines",
                                  "get_hybrid_search", "invalidate_search_cache",
                                  "get_index_status", "load_template")),
        ("modules.search_unified", ("search_articles", "_get_cached_search",
                                    "_set_cached_search", "_zim_search_fallback",
                                    "_book_search_fallback", "_collection_search_fallback",
                                    "search")),
        ("modules.tts_plugins", ("plugins_list", "handle_tts_voices",
                                 "_list_silero_speakers", "build_extension_loader")),
        ("modules.misc_runtime", ("_warmup_tts_later", "_check_lm_studio_status")),
        ("modules.laya_guards", ("_laya_books_enabled", "_laya_guard_context")),
        ("modules.notes_exports", ("_export_note_as_pdf", "_export_note_as_docx",
                                   "_extract_text_from_pdf")),
        ("modules.tts_engine", ("_get_piper_model_dir", "_get_piper_cache_dir",
                                "_find_piper_model", "_list_piper_voices",
                                "_get_piper_voice", "_get_silero_hub_dir",
                                "_load_silero_v5", "_init_tts_background",
                                "_ensure_tts_ready", "_start_tts_init_async",
                                "_init_qwen3", "_qwen3_synthesize",
                                "_apply_silero_rate_pitch",
                                "_apply_silero_rate_pitch_to_wav",
                                "_publish_tts_state", "_qwen3_port_open")),
        ("modules.news_schedulers", ("_ensure_news_schedulers",
                                     "_start_news_schedulers_later")),
        ("modules.tts_installers", ("handle_install_piper", "handle_install_silero",
                                    "_piper_download", "_piper_install_dir")),
        ("modules.app_processes", ("start_app_terminal", "start_app_server",
                                   "send_app_terminal_input", "stop_app_terminal",
                                   "stop_app_server", "get_app_server_status",
                                   "get_app_terminal_output",
                                   "_start_app_terminal_windows")),
        ("modules.article_ask", ("ask_article_question", "_budget_history_messages")),
        ("modules.index_writer", ("_run_index_background", "rebuild_search_index",
                                  "_start_background_indexing")),
        ("modules.profile_state", ("apply_profile_globals",))
    )
    bound = []
    pushed = 0
    for mod_name, names in _ports:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            _original.logger.warning(f"[native port] {mod_name} не импортирован: {e}")
            continue
        for n in names:
            if not hasattr(mod, n):
                continue
            fn = getattr(mod, n)
            globals()[n] = fn
            bound.append(n)
            # Подмодули пакета на уровне импорта делают by-value копию
            # (`X = _original.X`), поэтому их собственные вызовы всё ещё шли бы
            # в монолит. Подменяем именно те имена, что указывают на монолит.
            for sub in list(sys.modules.values()):
                if not getattr(sub, "__name__", "").startswith(__name__ + "."):
                    continue
                cur = getattr(sub, n, None)
                if cur is not None and getattr(cur, "__module__", "") == _original.__name__:
                    setattr(sub, n, fn)
                    pushed += 1
    _original.logger.info(f"[native port] пересажено имён: {len(bound)}, "
                          f"копий в подмодулях заменено: {pushed}")
    return bound


_native_ported = _apply_native_ports()
del _apply_native_ports

# Сохраняем ссылку на оригинальный модуль для отладки
_ORIGINAL = _original

del _attr_name, _exclude, _dot_modules, _config_mod, _templates_mod
del _search_mod, _chat_mod, _tts_mod, _media_mod, _notes_mod, _books_mod, _social_mod, _education_mod, _torrent_mod, _auth_mod
del _name
