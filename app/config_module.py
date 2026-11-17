#!/usr/bin/env python3
"""
Модуль конфигурации Infinity AI Search System.

Загружает config.json с fallback на дефолты.
Поддерживает переменные окружения для секретов.
"""

import os
import re
import json
import swallow

# ─── Переносимая сборка (portable) ────────────────────────────────────────────
# В портативной сборке рядом с каталогом кода лежат:
#     <корень>/app     — код (этот каталог)
#     <корень>/models  — модели STT/TTS и gguf
#     <корень>/engine  — llama-server и его dll
#     <корень>/data    — индексы, заметки, сессии
# Пути в config.json можно писать относительными («models», «data/notes») или
# через плейсхолдеры ${ROOT} / ${APP} — они разворачиваются в корень сборки и
# каталог кода. Корень переопределяется переменной INFINITY_ROOT.

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


def _root():
    """Корень переносимой сборки (либо каталог приложения в обычном запуске)."""
    env = os.environ.get("INFINITY_ROOT")
    if env:
        return os.path.abspath(env)
    parent = os.path.dirname(_APP_DIR)
    for marker in ("models", "engine", "data"):
        if os.path.isdir(os.path.join(parent, marker)):
            return parent
    return _APP_DIR


def app_root():
    """Публичный доступ к корню сборки."""
    return _root()


def app_dir():
    """Каталог с кодом приложения."""
    return _APP_DIR


# Интеграция с профилями (ленивый импорт)
_profiles_module = None
def _get_profiles():
    global _profiles_module
    if _profiles_module is None:
        try:
            import profiles_module as pm
            _profiles_module = pm
        except ImportError:
            swallow.note('config_module._get_profiles:L58')
            _profiles_module = False
    return _profiles_module if _profiles_module else None

# Дефолтные значения. Пути — переносимые: ${ROOT} разворачивается в корень
# сборки (см. _root()), поэтому дефолты одинаково работают и при обычном
# запуске, и в переносимой сборке на флешке.
_DEFAULTS = {
    "data_dirs": [],
    "books_dir": "${ROOT}/data/books",
    "books_db_path": "",
    "notes_dir": "${ROOT}/data/notes",
    "ai_dir": "${ROOT}/data/notes/_ai_answers",
    "memory_dir": "${ROOT}/data/notes/_memory",
    "attachments_dir": "${ROOT}/data/notes/_attachments",
    "plugins_dir": "${ROOT}/data/notes/_plugins",
    "chat_dir": "${ROOT}/data/notes/_chat_sessions",
    "html_viewer_dirs": ["${ROOT}/data/html"],
    "apps_dirs": ["${ROOT}/apps", "${APP}/filemanage_module_app"],
    "apps_dir": "",
    "default_apps_dir": "defaultapps",
    "zim_dirs": ["${ROOT}/data/zim"],

    "social_domain": "",

    # Бэкенд по умолчанию — llama.cpp (llama-server). В портативной сборке
    # движок поднимает engine\llama-server.exe, а LM Studio в сборке не
    # установлен и не запущен. Прежде здесь стоял "lmstudio": профили
    # наследовали его при init_profiles, и прокси /openai/v1 уходил на
    # localhost:1234 вместо живого :8080 — агент получал ошибку вместо
    # текста. Если у вас реально LM Studio — поменяйте backend в
    # config.json конкретного профиля.
    "backend": "llamacpp",

    "llama_cpp": {
        "url": "http://127.0.0.1:8080",
        "port": 8080
    },

    # Список облачных провайдеров ( OpenAI-совместимые API ). Активный
    # выбирается по id; секция custom — его проекция для server.CUSTOM_*.
    "custom_providers": [],
    "custom_active": "",
    "custom": {
        "url": "",
        "api_key": "",
        "model": ""
    },

    "lm_studio": {
        "url": "http://localhost:1234",
        "model": "gemma-3-1b-it@q8_0",
        "embed_model": "nomic-ai/nomic-embed-text-v1.5-GGUF",
        "api_key_env": "LM_STUDIO_API_KEY",
        "timeout": 30,
        "max_tokens_default": 4096,
        "context_tokens_default": 16000,
        "temperature_default": 0.7,
        "enable_thinking_default": False
    },

    "server": {
        # Локальное приложение по умолчанию не выходит в сеть: у API нет
        # аутентификации, и 0.0.0.0 отдавал профили и файлы всей локальной
        # сети. Для доступа с других устройств хост задаётся явно.
        "host": "127.0.0.1",
        "port": 8181,
        "workers": 1
    },

    "auth": {
        "enabled": False,
        "username": "admin",
        "password_hash": "",
        "token": ""
    },

    # Мастер первой настройки и интерактивный тур. Выключаются в админ-панели;
    # состояние «пройдено» хранится отдельно в каждом профиле.
    "onboarding": {
        "wizard_enabled": True,
        "tour_enabled": True,
    },

    # Сжатие пользовательских данных (zstandard). По умолчанию выключено:
    # включение — отдельный рубильник в админ-панели, модули выбираются там же.
    "compression": {
        "enabled": False,
        "modules": {"media": False, "books": False, "files": False, "notes": False},
        "level": 10,
        "min_size_kb": 4,
        "max_file_mb": 256,
        "interval_min": 180,
    },

    "search": {
        "limit": 15,
        "use_embeddings": True,
        "bm25_limit": 50,
        "faiss_limit": 50,
        "cross_encoder_enabled": True,
        "spell_correction_enabled": True,
        "recency_boost_enabled": True
    },

    "index": {
        "dir": "${ROOT}/data/index",
        "faiss_index_path": "${ROOT}/data/index/faiss.index",
        "embedding_db_path": "${ROOT}/data/index/embeddings.db",
        "dimension": 768
    },

    "ui": {
        "theme": "dark",
        "font_size": 17,
        "line_height": 1.85,
        "max_width": 780,
        "language": "ru",
        "speech": {
            "selection_tts_enabled": True,
            "voice_input_enabled": True
        }
    },

    # Централизованные настройки распознавания и синтеза речи.
    # tts_engine: auto | silero | piper | qwen3 | webspeech | custom:<id>
    # stt_engine: local | openai | custom:<id>
    "speech": {
        "tts_engine": "auto",
        "tts": {
            "silero_speaker": "xenia",
            "piper_voice": "",
            "qwen3_url": "http://127.0.0.1:7860",
            "qwen3_speaker": "",
            "rate": 1.0,
            "pitch": 0.0,
            # 0 = авто (2-4 процесса по числу ядер), см. silero_pool.default_workers.
            "silero_workers": 0,
        },
        "stt_engine": "local",
        "stt": {
            # Пустая local_model = «авто»: stt_engine.resolve_model берёт модель
            # по profile и устройству (CPU -> base, CUDA -> turbo). Раньше здесь
            # стоял "turbo", из-за чего на CPU фраза в 3 секунды читалась 16 секунд.
            "local_model": "",
            # Необязательный speech.stt.profile переопределяет автоматический
            # выбор: fast -> base, balanced -> small, accurate -> turbo.
            # Если ключа нет, модель выбирается по устройству (CPU -> base,
            # CUDA -> turbo) — см. stt_engine.resolve_model.
            "local_language": "ru",
            "local_device": "auto",
            "local_compute_type": "auto",
            # 0 = авто (половина ядер, не больше 16); ctranslate2 при значении
            # по умолчанию фактически берёт только 4 потока.
            "local_threads": 0,
            "models_dir": "${ROOT}/models/whisper",
            "auto_download": False,
            "openai_base_url": "https://api.openai.com/v1",
            "openai_api_key": "",
            "openai_model": "whisper-1",
        },
        "custom_stt_engines": [],
        "custom_tts_engines": [],
        "selection_tts_enabled": True,
        "voice_input_enabled": True,
    },

    "research": {
        "iterations": 15,
        "research_model": "gemma-3-4b-it",
        "final_model": "qwen3.5-30b-a3b",
        "max_final_tokens": 20000,
        "small_model": "gemma-3-1b-it@q8_0"
    },

    "logging": {
        "level": "INFO",
        "file": "~/.infinity/server.log",
        "max_bytes": 10485760,
        "backup_count": 5
    },

    "stats": {
        "enabled": True,
        "recommendations_enabled": True,
        "notifications_enabled": True,
        "max_notifications_per_day": 3,
        "ai_recommendations": False
    },
    "media": {
        "music_dirs": [],
        "video_dirs": [],
        "photo_dirs": [],
        "auto_index": False,
        "fetch_lyrics": True,
        "fetch_artist_info": True
    },

    # Автообновление приложений из defaultapps по GitHub-релизам.
    # Всё выключено по умолчанию: сеть и запись в defaultapps начинаются
    # только после явного включения в админке (вкладка «Приложения»).
    "apps": {
        "auto_check": False,
        "auto_update": False,
        "notify_updates": False,
        "check_interval_hours": 24,
    },

    "network": {
        "tor": {
            "enabled": False,
            "control_port": 9051,
            "socks_port": 9050,
            "onion_port": 80
        },
        "zapret": {
            "enabled": False
        },
        "coordination": {
            "enabled": False,
            "urls": [],
            "node_name": "",
            "public_url": "",
            "announce": True,
            "publish_profile": True
        }
    },

    # Движок решений laya («System 1»): типизированные ответы по тексту без
    # генерации. Выключен по умолчанию; каждая функция — под своим флагом.
    "laya": {
        "enabled": False,
        "url": "http://127.0.0.1:8377",
        "python": "",
        "autostart": False,
        "model": "multilingual",
        "device": "cpu",
        "max_loaded": 1,
        "idle_unload_seconds": 900,
        "timeout_ms": 1500,
        "max_chars": 4000,
        "api_key": "",
        "log_level": "warning",
        "hf_home": "${ROOT}/models",
        "features": {
            "news_semantic": False,
            "news_briefing_fast": False,
            "social_triage": False,
            "media_autotag": False,
            "media_recommend": False,
            "books_classify": False,
            "voice_intent": False,
            "guard_injection": False,
            "bot_triage": False,
            "files_category": False,
            "notes_auto_tags": False,
            "notes_semantic_groups": False,
            "education_auto_terms": False,
            "extensions_access_warning": False,
            "search_route": False,
            "mcp": False
        }
    },

    # GenLab — AI-генератор (ComfyUI). Пути хранятся переносимо:
    # "${ROOT}/ComfyUI_windows_portable" — см. genlab/config.comfy_root().
    # models_dir — своя папка с весами (можно на другом диске); ComfyUI
    # подключается к ней через генерируемый extra_model_paths.yaml.
    # Ничего не скачивается: модуль только читает то, что положил пользователь.
    "genlab": {
        "enabled": True,
        "autostart": False,
        "comfy_root": "",
        "models_dir": "",
        "auto_install_packs": False,
        "packs": [],
        "host": "127.0.0.1",
        "port": 8188,
        "max_queue": 64,
        "max_parallel": 1,
        "timeout_sec": 3600,
        "default_checkpoint": "",
        "default_vae": "",
        "default_lora": "",
        "default_preset": "image.text2img",
        "autoplay_video": True,
        "thumbnail_px": 320,
        "history_limit": 500
    },
}


def _expand_path(path):
    """Раскрыть путь: ~, переменные окружения, ${ROOT}/${APP} и относительные.

    Относительный путь («models», «data/notes») разворачивается от корня
    переносимой сборки — поэтому config.json переносится на флешку как есть.
    URL и пути, начинающиеся с «/», не трогаем.
    """
    if not path or not isinstance(path, str):
        return path
    if _URL_RE.match(path):
        return path
    # Конфиг мог быть сохранён на другой ОС (Windows ↔ WSL): приводим
    # /mnt/e/... → E:\... и обратно, иначе каталоги не находятся.
    try:
        from path_compat import to_native as _to_native
        path = _to_native(path)
        if not path or not isinstance(path, str):
            return path
    except Exception:
        swallow.note('config_module._expand_path:L369')
        pass
    p = os.path.expanduser(os.path.expandvars(path))
    was_posix = p.startswith("/")          # «/hermes», «/data/notes» — префиксы
    p = p.replace("${ROOT}", _root()).replace("${APP}", _APP_DIR)
    if p and not os.path.isabs(p) and not was_posix:
        p = os.path.join(_root(), p)
    if not p:
        return p
    p = os.path.normpath(p)
    if was_posix:
        # normpath на Windows превращает «/hermes» в «\hermes», а такие
        # значения уходят в Location-заголовки и URL — там нужен прямой слэш.
        p = p.replace("\\", "/")
    return p


def to_portable(path):
    """Абсолютный путь ВНУТРИ сборки — в переносимую запись «${ROOT}/...».

    Назначение: конфиг не должен запекать букву диска и папку конкретного
    компьютера. Всё, что лежит внутри сборки, записается плейсхолдером, и
    после переноса папки на флешку или на другой диск конфиг остаётся
    рабочим без миграции.

    Наружу (книги на другом диске, wiki, модели) возвращаем как есть:
    это пользовательские внешние каталоги, их переносить нельзя.
    Относительные пути и готовые плейсхолдеры тоже не трогаем — они уже
    переносимые.
    """
    if not path or not isinstance(path, str):
        return path
    raw = str(path)
    if "${ROOT}" in raw or "${APP}" in raw:
        return raw
    try:
        if not os.path.isabs(raw):
            return raw
    except Exception:
        swallow.note('config_module.to_portable:L408')
        return raw
    try:
        root = os.path.normpath(_root())
        target = os.path.normpath(raw)
        # Сравнение без учёта регистра: на Windows 'E:\\Build' и 'e:\\build'
        # — один и тот же путь, и регистр может разойтись между местами,
        # откуда пришли строки.
        if os.name == "nt":
            low_t, low_r = target.lower(), root.lower()
        else:
            low_t, low_r = target, root
        if low_t == low_r:
            return "${ROOT}"
        if not low_t.startswith(low_r + os.sep):
            return raw
        rel = target[len(root):].lstrip(os.sep).replace("\\", "/")
        return "${ROOT}/" + rel
    except Exception:
        swallow.note('config_module.to_portable:L427')
        return raw


def is_portable_token(value):
    """Уже ли значение переносимой записью (плейсхолдер или относительный путь)."""
    if not isinstance(value, str) or not value:
        return False
    if "${ROOT}" in value or "${APP}" in value:
        return True
    try:
        return not os.path.isabs(value)
    except Exception:
        swallow.note('config_module.is_portable_token:L440')
        return False


def _looks_like_path(v):
    """Похоже ли значение на путь файловой системы (а не на «модель», «движок»).

    Одиночное слово («models», «engine», «data») считаем путём только если такой
    каталог или файл реально есть рядом с приложением или в корне сборки —
    иначе «gemma-3-1b-it@q8_0» превратился бы в путь.
    """
    if not v or _URL_RE.match(v):
        return False
    if any(t in v for t in ("~", "/", "\\", "${")):
        return True
    return (os.path.exists(os.path.join(_root(), v))
            or os.path.exists(os.path.join(_APP_DIR, v)))


def _expand_paths_in_dict(d):
    """Раскрыть пути во всех строковых значениях словаря (рекурсивно)."""
    if not isinstance(d, dict):
        return d
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _expand_path(v) if _looks_like_path(v) else v
        elif isinstance(v, list):
            result[k] = [_expand_path(x) if isinstance(x, str) and _looks_like_path(x)
                         else x for x in v]
        elif isinstance(v, dict):
            result[k] = _expand_paths_in_dict(v)
        else:
            result[k] = v
    return result


def _deep_merge(base, override):
    """Глубокое слияние словарей. override имеет приоритет."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def normalize_openai_base(url):
    """База OpenAI-совместимого API без хвостового /v1 и лишних слешей.

    К базе добавляется /v1/chat/completions, а страница настроек раньше
    подсказывала вводить адрес «с /v1 на конце» — сохранённый
    https://api.openai.com/v1 давал /v1/v1/chat/completions и 404.
    """
    u = str(url or "").strip().rstrip("/")
    if u.lower().endswith("/v1"):
        u = u[:-3].rstrip("/")
    return u


def sync_cloud_providers(cfg):
    """Свести список облачных провайдеров и активный выбор к одному виду.

    Источник правды — custom_providers + custom_active. Секция custom
    остаётся проекцией активного провайдера: её читает server_backup
    (CUSTOM_*) и модули, которым нужен готовый url/model. Конфиг без
    списка (старый, один провайдер в custom) не теряется.
    """
    if not isinstance(cfg, dict):
        return cfg
    providers = []
    seen = set()
    for i, item in enumerate(cfg.get("custom_providers") or []):
        if not isinstance(item, dict):
            continue
        url = normalize_openai_base(item.get("url"))
        if not url:
            continue
        pid = str(item.get("id") or "").strip() or "cp-%d" % (i + 1)
        if pid in seen:
            pid = "%s-%d" % (pid, i + 1)
        seen.add(pid)
        providers.append({
            "id": pid,
            "name": str(item.get("name") or "").strip() or url,
            "url": url,
            "api_key": str(item.get("api_key") or ""),
            "model": str(item.get("model") or "").strip(),
        })
    cfg["custom_providers"] = providers

    legacy = cfg.get("custom") or {}
    if not isinstance(legacy, dict):
        legacy = {}
    if not providers:
        cfg["custom_active"] = ""
        cfg["custom"] = {
            "url": normalize_openai_base(legacy.get("url")),
            "api_key": str(legacy.get("api_key") or ""),
            "model": str(legacy.get("model") or "").strip(),
        }
        return cfg

    active = str(cfg.get("custom_active") or "").strip()
    chosen = next((p for p in providers if p["id"] == active), None)
    if chosen is None:
        legacy_url = normalize_openai_base(legacy.get("url"))
        chosen = next((p for p in providers if p["url"] == legacy_url), None)
    chosen = chosen or providers[0]
    cfg["custom_active"] = chosen["id"]
    cfg["custom"] = {k: chosen[k] for k in ("url", "api_key", "model")}
    return cfg



def _get_env_secret(config):
    """Получить API-ключ из переменных окружения."""
    env_var = config.get("lm_studio", {}).get("api_key_env", "LM_STUDIO_API_KEY")
    return os.environ.get(env_var, "")


def load_config(config_path=None):
    """
    Загрузить конфигурацию.

    Приоритет:
    1. Если доступна система профилей — конфиг активного профиля
    2. INFINITY_CONFIG env var → config.json → ~/.infinity/config.json

    Args:
        config_path: Путь к файлу конфигурации (игнорируется при использовании профилей).

    Returns:
        dict: Словарь конфигурации с раскрытыми путями.
    """
    # Пробуем загрузить через профили
    pm = _get_profiles()
    if pm:
        profile_config = pm.get_active_effective_config()
        if profile_config:
            config = _deep_merge(_DEFAULTS, profile_config)
            config = _expand_paths_in_dict(config)
            return config
        else:
            # Профили есть, но конфиг не загрузился — инициализируем
            pass

    # Fallback: загрузка из config.json
    if config_path is None:
        config_path = os.environ.get("INFINITY_CONFIG")
        if not config_path:
            local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            if os.path.exists(local_path):
                config_path = local_path
            else:
                config_path = os.path.expanduser("~/.infinity/config.json")

    config = _DEFAULTS.copy()

    if os.path.exists(config_path):
        try:
            # utf-8-sig, а не utf-8: Windows-редакторы (Notepad, WordPad,
            # часть скриптов сборки) пишут JSON с BOM. Обычный utf-8 такой
            # файл не читает — json.load падает на «Unexpected UTF-8 BOM»,
            # и load_config МОЛЧА уходит на значения по умолчанию.
            # В данной сборке это означало: backend=llamacpp из config.json
            # игнорировался, прокси /openai/v1/chat/completions уходил на
            # LM Studio (localhost:1234) вместо живого llama-server на 8080,
            # агент получал «All connection attempts failed» и НИЧЕГО не
            # показывал — ни текста, ни вызовов инструментов.
            with open(config_path, "r", encoding="utf-8-sig") as f:
                user_config = json.load(f)
            config = _deep_merge(_DEFAULTS, user_config)
        except (json.JSONDecodeError, IOError) as e:
            print(f"WARNING: Ошибка загрузки config.json ({config_path}): {e}")
            print("Используются настройки по умолчанию.")

    # Если профили доступны — инициализируем и сохраняем
    pm = _get_profiles()
    if pm:
        pm.init_profiles(source_config=config)
        profile_config = pm.get_active_effective_config()
        if profile_config:
            config = _deep_merge(_DEFAULTS, profile_config)

    # Поддержка AppImage: если writable директории недоступны — используем data_dir
    data_dir = os.environ.get("INFINITY_DATA_DIR")
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        
        def _needs_redirect(path, default_subdir):
            """Нужно ли перенаправить путь?"""
            if not path:
                return True
            path = os.path.expanduser(path)
            # Путь не существует или read-only
            if not os.path.exists(path):
                return True
            # Путь внутри AppImage (/.mount_)
            if "/.mount_" in path:
                return True
            # Нет прав на запись
            if not os.access(path, os.W_OK):
                return True
            return False
        
        writable_map = {
            "notes_dir": "notes",
            "ai_dir": "notes/_ai_answers",
            "memory_dir": "notes/_memory",
            "attachments_dir": "notes/_attachments",
            "plugins_dir": "notes/_plugins",
            "chat_dir": "notes/_chat_sessions",
        }
        for key, subdir in writable_map.items():
            if key in config and _needs_redirect(config[key], subdir):
                config[key] = os.path.join(data_dir, subdir)
                os.makedirs(config[key], exist_ok=True)
        
        # Index
        idx = config.get("index", {})
        if "dir" in idx and _needs_redirect(idx["dir"], "index"):
            config["index"]["dir"] = os.path.join(data_dir, "index")
            config["index"]["faiss_index_path"] = os.path.join(data_dir, "index", "faiss.index")
            config["index"]["embedding_db_path"] = os.path.join(data_dir, "index", "embeddings.db")
            os.makedirs(config["index"]["dir"], exist_ok=True)
        
        # Проверяем что хотя бы notes_dir доступен
        if config.get("notes_dir") and not os.path.exists(config["notes_dir"]):
            config["notes_dir"] = os.path.join(data_dir, "notes")
            os.makedirs(config["notes_dir"], exist_ok=True)

    # Раскрыть ~ в путях
    config = _expand_paths_in_dict(config)

    return config


def get_api_key(config):
    """Получить API-ключ из окружения или конфига."""
    env_key = _get_env_secret(config)
    if env_key:
        return env_key
    # Fallback: если ключ прямо в конфиге (не рекомендуется)
    return config.get("lm_studio", {}).get("api_key", "")


def save_config(config, config_path=None):
    """
    Сохранить конфигурацию.

    Приоритет:
    1. Если доступна система профилей — сохраняем в активный профиль
    2. В config.json

    Args:
        config: Словарь конфигурации.
        config_path: Путь для сохранения (игнорируется при использовании профилей).
    """
    pm = _get_profiles()
    if pm:
        pm.save_active_config(config)
        return

    if config_path is None:
        config_path = os.environ.get("INFINITY_CONFIG")
        if not config_path:
            local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            if os.path.exists(local_path):
                config_path = local_path
            else:
                config_path = os.path.expanduser("~/.infinity/config.json")

    config_dir = os.path.dirname(config_path)
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)

    # Атомарно: обычный open(...,"w") обрубал config.json, и при убийстве
    # процесса (recovery_guard, Ctrl+C) настройки пропадали целиком.
    tmp = "%s.tmp-%d" % (config_path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            swallow.note('config_module.save_config:L732')
            pass
        raise

    return config_path


def load_config_for(profile_id):
    """Конфиг конкретного профиля, собранный так же, как load_config.

    Глобальный профиль сервера при этом может быть другим: страница настроек
    принадлежит тому, кто прислал cookie. Без этого GET /api/config отдавал
    одному профилю значения другого (замер 2026-10-27: ответы для cookie
    admin, default и bogus побайтово совпадали).
    """
    pm = _get_profiles()
    if pm and profile_id:
        try:
            profile_config = pm.get_effective_config(profile_id)
        except Exception:
            swallow.note('config_module.load_config_for:L752')
            profile_config = None
        if profile_config:
            config = _deep_merge(_DEFAULTS, profile_config)
            return _expand_paths_in_dict(config)
    return load_config()


def save_config_for(config, profile_id):
    """Сохранить конфиг конкретного профиля (см. load_config_for)."""
    pm = _get_profiles()
    if pm and profile_id:
        try:
            return bool(pm.save_profile_config(profile_id, config))
        except Exception:
            swallow.note('config_module.save_config_for:L767')
            return False
    save_config(config)
    return True


# Глобальный экземпляр конфигурации (ленивая инициализация)
_config = None


def get_config():
    """Получить глобальную конфигурацию (ленивая загрузка)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config():
    """Перезагрузить конфигурацию."""
    global _config
    _config = load_config()
    return _config


def get_speech(config=None):
    """Вернуть централизованные настройки речи с фоллбэком на legacy ui.speech.

    Результат сливается с дефолтами секции 'speech', чтобы старые конфиги,
    где есть только ui.speech.selection_tts_enabled/voice_input_enabled,
    продолжали работать.
    """
    if config is None:
        config = get_config()
    speech = dict((_DEFAULTS.get("speech") or {}))
    user_speech = config.get("speech") or {}
    if isinstance(user_speech, dict):
        speech = _deep_merge(speech, user_speech)
    # Legacy ui.speech — маппинг на новые ключи
    legacy = (config.get("ui") or {}).get("speech") or {}
    if isinstance(legacy, dict):
        # ui.speech имеет приоритет только если speech.* не задан явно
        if "selection_tts_enabled" not in user_speech:
            speech["selection_tts_enabled"] = legacy.get("selection_tts_enabled", True)
        if "voice_input_enabled" not in user_speech:
            speech["voice_input_enabled"] = legacy.get("voice_input_enabled", True)
    return speech
