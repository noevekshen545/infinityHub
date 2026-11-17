"""
GenLab — конфигурация модуля и портативный резолв путей.

Настройки живут в секции ``genlab`` файла config.json (общий механизм проекта).
Все пути поддерживают переносимые токены ``${ROOT}`` / ``${APP}`` — их
раскрывает ``config_module._expand_path()``, а при сохранении конфига
``config_module.to_portable_paths()`` снова превращает абсолютные пути внутри
сборки в токены. Поэтому каталог ComfyUI можно спокойно переносить на флешку.
"""

import contextlib
import contextvars
import os
import threading
import swallow

logger = None  # проставляется в _logger() — модуль импортируется раньше логгера

# Профиль текущего HTTP-запроса (или фонового задания). Без него модуль
# работал с профилем, активным в системе целиком: два браузера на разных
# профилях видели одни и те же проекты, а агент — данные не того профиля.
_PROFILE_CTX = contextvars.ContextVar("genlab_profile", default="")


@contextlib.contextmanager
def profile_scope(profile_id):
    """Выполнять код в data-каталоге указанного профиля."""
    token = _PROFILE_CTX.set(str(profile_id or "").strip())
    try:
        yield
    finally:
        _PROFILE_CTX.reset(token)


def current_profile_id():
    """Профиль запроса/задания; вне запроса — активный профиль сервера."""
    return _PROFILE_CTX.get() or _active_profile_id()

# Каталог ComfyUI под корнем сборки. Именно то, куда его положил пользователь:
# <корень>\ComfyUI_windows_portable
COMFY_DIRNAME = "ComfyUI_windows_portable"

# Фолбэк, если ComfyUI лежит вне сборки (например старая установка на E:\)
LEGACY_ROOTS = (
    r"E:\ComfyUI_windows_portable",
    os.path.expandvars(r"%USERPROFILE%\ComfyUI_windows_portable"),
    os.path.expandvars(r"%USERPROFILE%\ComfyUI"),
)

DEFAULTS = {
    # Включён ли модуль (роутер подключён всегда, но UI прячет разделы)
    "enabled": True,
    # Автозапуск процесса ComfyUI при старте сервера
    "autostart": False,
    # Пользовательская папка ComfyUI (пусто = искать автоматически).
    # Поддерживает переносимый токен ${ROOT}, см. comfy_root().
    "comfy_root": "",
    # Пользовательская папка с моделями. Пусто = <comfy_root>/ComfyUI/models.
    # Нужна, если веса лежат на другом диске или в общем хранилище.
    "models_dir": "",
    # Автоматически ставить кастом-паки ComfyUI (ControlNet Aux, IPAdapter+,
    # Impact, FaceRestore, ReActor). Модели при этом НЕ скачиваются.
    "auto_install_packs": False,
    # Разрешённые репозитории паков (список словарей: id, repo, title, why)
    "packs": [],
    "host": "127.0.0.1",
    "port": 8188,
    # Лимиты
    "max_queue": 64,
    "max_parallel": 1,
    "timeout_sec": 3600,
    # Модели по умолчанию для пресетов (пусто = авто-взять единственную)
    "default_checkpoint": "",
    "default_vae": "",
    "default_lora": "",
    # Интерфейс
    "default_preset": "image.text2img",
    "autoplay_video": True,
    "thumbnail_px": 320,
    "history_limit": 500,
}

_lock = threading.RLock()
_cache = {"value": None, "key": None}


def _logger():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("infinity.genlab")
    return logger


def _defaults():
    """Секция genlab из config_module._DEFAULTS + локальные значения по умолчанию."""
    merged = dict(DEFAULTS)
    try:
        from config_module import _DEFAULTS
        merged.update(_DEFAULTS.get("genlab") or {})
    except Exception:
        swallow.note('config._defaults:L101')
        pass
    return merged


def _global_active_id():
    try:
        from profiles_module import get_active_id
        return get_active_id() or ""
    except Exception:
        swallow.note('config._global_active_id:L111')
        return ""


def _config_snapshot(profile_id):
    """Конфиг профиля: активный читаем через config_module (кэш и раскрытие путей)."""
    pid = str(profile_id or "").strip()
    if pid and pid == _global_active_id():
        try:
            from config_module import get_config
            return get_config() or {}
        except Exception as e:
            _logger().warning(f"[genlab] не удалось прочитать конфиг: {e}")
    if pid:
        try:
            from profiles_module import get_profile_config
            cfg = get_profile_config(pid)
            if isinstance(cfg, dict):
                return cfg
        except Exception as e:
            _logger().warning(f"[genlab] конфиг профиля {pid} не прочитан: {e}")
        # У профиля ещё нет собственного файла — работаем на значениях по
        # умолчанию. Конфиг активного профиля подмешивать нельзя: пути и
        # настройки ComfyUI одного профиля утекли бы в другой.
        return {}
    try:
        from config_module import get_config
        return get_config() or {}
    except Exception:
        swallow.note('config._config_snapshot:L140')
        return {}


def get_settings():
    """Текущие настройки модуля (кэшируются по файловому ключу конфига)."""
    cfg = _config_snapshot(current_profile_id())

    key = _config_key()
    with _lock:
        if _cache["value"] is not None and _cache["key"] == key:
            return dict(_cache["value"])

    merged = _defaults()
    section = cfg.get("genlab")
    if isinstance(section, dict):
        for k, v in section.items():
            merged[k] = v

    with _lock:
        _cache["value"] = dict(merged)
        _cache["key"] = key
    return dict(merged)


def _config_key():
    """Ключ кэша — профиль + путь/mtime конфига (без чтения файла)."""
    pid = current_profile_id()
    path = None
    try:
        from profiles_module import _profile_config_path
        path = _profile_config_path(pid)
    except Exception:
        swallow.note('config._config_key:L173')
        path = None
    if not path or not os.path.isfile(path):
        # Профиль входит в ключ: иначе два профиля без своих файлов делили
        # бы один закэшированный результат.
        return ("profile", pid, None)
    try:
        st = os.stat(path)
        return ("profile", pid, path, st.st_mtime_ns, st.st_size)
    except OSError:
        swallow.note('config._config_key:L183')
        return ("profile", pid, None)


def save_settings(patch, profile_id=""):
    """Записать патч в секцию genlab конфига текущего профиля."""
    if not isinstance(patch, dict):
        raise ValueError("patch должен быть объектом")
    pid = str(profile_id or "").strip() or current_profile_id()
    try:
        import profiles_module
    except Exception as e:
        _logger().error(f"[genlab] profiles_module недоступен: {e}")
        return False

    cfg = _config_snapshot(pid)
    section = dict(cfg.get("genlab") or {})
    section.update(patch)
    # Путь сохраняем переносимым, чтобы не запечь букву диска
    if section.get("comfy_root"):
        try:
            from config_module import to_portable
            section["comfy_root"] = to_portable(section["comfy_root"])
        except Exception:
            swallow.note('config.save_settings:L207')
            pass
    cfg["genlab"] = section

    if pid == _global_active_id():
        profiles_module.save_active_config(cfg)
        # config_module кэширует весь конфиг, а get_settings() читает именно его.
        # Без перезагрузки только что сохранённая секция не проявится до рестарта.
        try:
            from config_module import reload_config
            reload_config()
        except Exception as e:
            _logger().warning(f"[genlab] не удалось перезагрузить конфиг: {e}")
    else:
        path = profiles_module._profile_config_path(pid)
        if not profiles_module._atomic_write_json(path, cfg):
            return False
    invalidate()
    return True


def invalidate():
    """Сбросить кэш настроек (после смены профиля или правки конфига)."""
    with _lock:
        _cache["value"] = None
        _cache["key"] = None


# --------------------------------------------------------------------------
# Пути
# --------------------------------------------------------------------------

def app_root():
    """Корень переносимой сборки (там, где models/ engine/ data/)."""
    try:
        from config_module import _root
        return _root()
    except Exception:
        swallow.note('config.app_root:L245')
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expand_path(path):
    """Раскрыть переносимый путь через config_module (${ROOT}, ~, WSL и т.п.)."""
    if not path or not isinstance(path, str):
        return ""
    try:
        from config_module import _expand_path
        return _expand_path(path)
    except Exception:
        swallow.note('config.expand_path:L257')
        p = os.path.expanduser(os.path.expandvars(path))
        p = p.replace("${ROOT}", app_root())
        if not os.path.isabs(p):
            p = os.path.join(app_root(), p)
        return os.path.normpath(p)


def detect_comfy_root():
    """Найти ComfyUI без учёта настройки comfy_root (чистый автопоиск).

    Нужен, чтобы отличать «пользователь сам указал путь» от «мы его нашли»:
    если пользователь ввёл тот же путь, что нашёл автопоиск, сохранять его
    не нужно — пустое значение переживёт перенос сборки на другой диск.
    """
    env = (os.environ.get("INFINITY_COMFY_ROOT") or "").strip()
    if env:
        return os.path.normpath(env)

    candidate = os.path.join(app_root(), COMFY_DIRNAME)
    if _looks_like_comfy(candidate):
        return candidate

    for legacy in LEGACY_ROOTS:
        if legacy and _looks_like_comfy(legacy):
            return legacy
    return candidate


def comfy_root():
    """Каталог переносимой сборки ComfyUI (то, где python_embeded + ComfyUI/main.py).

    Приоритет:
      1. genlab.comfy_root из конфига (можно задать ${ROOT}/...)
      2. переменная окружения INFINITY_COMFY_ROOT
      3. ${ROOT}/ComfyUI_windows_portable   ← основной вариант
      4. легаси-пути (E:\\ComfyUI_windows_portable и др.)
    """
    cfg = get_settings()
    configured = (cfg.get("comfy_root") or "").strip()
    if configured:
        p = expand_path(configured)
        if p:
            return p

    found = detect_comfy_root()
    if _looks_like_comfy(found):
        inside = os.path.normcase(found).startswith(os.path.normcase(app_root()))
        if not inside:
            _logger().warning(
                f"[genlab] ComfyUI найден вне сборки: {found} — укажите "
                f"genlab.comfy_root, иначе сборка не будет переносимой")
    return found


def _looks_like_comfy(path):
    try:
        return (
            os.path.isfile(os.path.join(path, "ComfyUI", "main.py"))
            and os.path.isfile(os.path.join(path, "python_embeded", "python.exe"))
        )
    except (TypeError, ValueError):
        swallow.note('config._looks_like_comfy:L319')
        return False


def comfy_available():
    return _looks_like_comfy(comfy_root())


def comfy_python():
    return os.path.join(comfy_root(), "python_embeded", "python.exe")


def comfy_main():
    return os.path.join(comfy_root(), "ComfyUI", "main.py")


def comfy_base_url():
    cfg = get_settings()
    return "http://%s:%s" % (cfg.get("host") or "127.0.0.1", int(cfg.get("port") or 8188))


def models_dir():
    """Папка с весами моделей.

    Приоритет:
      1. genlab.models_dir из конфига — пользовательская папка (может быть на
         другом диске, поэтому хранится как есть, а не через ${ROOT});
      2. <comfy_root>/ComfyUI/models — обычное расположение ComfyUI.

    Именно эту папку читает ComfyUI при запуске: если она отличается от
    стандартной, её нужно указать ComfyUI через extra_model_paths.yaml,
    см. genlab.packs.write_extra_model_paths().
    """
    configured = (get_settings().get("models_dir") or "").strip()
    if configured:
        path = expand_path(configured)
        if path:
            return path
    return os.path.join(comfy_root(), "ComfyUI", "models")


def models_dir_is_custom():
    """True — папка моделей задана пользователем и отличается от стандартной."""
    return bool((get_settings().get("models_dir") or "").strip())


# Канонические имена папок моделей ComfyUI (folder_paths.folder_names_and_paths).
# Список снят с установленной версии 0.39.0 — при расхождении лишние ключи
# просто игнорируются, а недостающие папки ComfyUI создаёт сам.
MODEL_FOLDERS = [
    "checkpoints", "configs", "loras", "vae", "text_encoders",
    "diffusion_models", "clip_vision", "style_models", "embeddings",
    "diffusers", "vae_approx", "controlnet", "gligen", "upscale_models",
    "latent_upscale_models", "hypernetworks", "photomaker", "classifiers",
    "model_patches", "audio_encoders", "background_removal",
    "frame_interpolation", "geometry_estimation", "optical_flow", "detection",
]


def extra_model_paths_file():
    """Путь к сгенерированному extra_model_paths.yaml (в каталоге модуля).

    Мы НЕ правим файл внутри ComfyUI: конфиг генерируется у нас и передаётся
    через --extra-model-paths-config. Так пользовательская установка ComfyUI
    остаётся нетронутой, а свои папки моделей подключаются явно.
    """
    d = storage_root()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "extra_model_paths.yaml")


def write_extra_model_paths():
    """Сгенерировать YAML с пользовательской папкой моделей.

    Возвращает путь к файлу либо '' , когда внешняя папка не задана.
    Пути в YAML абсолютные: relative-разрешение в ComfyUI идёт от папки самого
    YAML, и это источник тихих ошибок, поэтому относительные пути не используем.
    """
    if not models_dir_is_custom():
        # Внешняя папка снята — удаляем конфиг, чтобы ComfyUI не продолжал
        # искать веса по уже недействующему пути.
        existing = extra_model_paths_file()
        if os.path.isfile(existing):
            try:
                os.remove(existing)
                _logger().info("[genlab] extra_model_paths.yaml удалён")
            except OSError as e:
                _logger().warning("[genlab] не удалось удалить %s: %s", existing, e)
        return ""
    root = models_dir()
    if not root:
        return ""
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        _logger().warning("[genlab] не удалось создать папку моделей %s: %s", root, e)

    lines = [
        "# Сгенерировано InfinitySearch GenLab. Не редактируйте вручную:",
        "# изменения перезаписываются при каждом запуске ComfyUI.",
        "genlab:",
        "    is_default: true",
    ]
    for folder in MODEL_FOLDERS:
        path = os.path.join(root, folder)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            swallow.note('config.write_extra_model_paths:L427')
            continue
        lines.append("    %s: %s" % (folder, _yaml_path(path)))
    text = "\n".join(lines) + "\n"

    target = extra_model_paths_file()
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except OSError as e:
        _logger().error("[genlab] не удалось записать %s: %s", target, e)
        return ""
    _logger().info("[genlab] внешняя папка моделей подключена: %s", root)
    return target


def _yaml_path(path):
    """Экранировать путь для YAML-скаляра (Windows-обратные слэши ломают парсер)."""
    return '"%s"' % path.replace("\\", "/").replace('"', '\\"')


def comfy_extra_args():
    """Дополнительные аргументы запуска ComfyUI, связанные с путями."""
    args = []
    config = write_extra_model_paths()
    if config:
        args += ["--extra-model-paths-config", config]
    return args


def comfy_custom_nodes_dir():
    return os.path.join(comfy_root(), "ComfyUI", "custom_nodes")


def comfy_input_dir():
    return os.path.join(comfy_root(), "ComfyUI", "input")


def comfy_output_dir():
    return os.path.join(comfy_root(), "ComfyUI", "output")


def comfy_python_embeded_dir():
    return os.path.join(comfy_root(), "python_embeded")


def storage_root(profile_id=""):
    """Корневая папка модуля в профиле: data/profiles/<id>/data/genlab/."""
    base = None
    try:
        from profiles_module import _profile_data_path
        base = _profile_data_path(str(profile_id or "").strip() or current_profile_id())
    except Exception:
        swallow.note('config.storage_root:L482')
        base = None
    if not base:
        try:
            from config_module import _root
            base = os.path.join(_root(), "data")
        except Exception:
            swallow.note('config.storage_root:L489')
            base = os.path.join(app_root(), "data")
    return os.path.join(base, "genlab")


def _active_profile_id():
    """Id активного профиля (для выбора каталога data/profiles/<id>/data/genlab/).

    get_active_id() возвращает ровно тот же id, что был бы у
    get_active_profile(), но без deepcopy индекса профилей: этот хелпер
    вызывают на каждый get_settings(), а тот — по нескольку раз за запрос.
    """
    try:
        from profiles_module import get_active_id, resolve_profile_id
        return resolve_profile_id(get_active_id()) or "default"
    except Exception:
        swallow.note('config._active_profile_id:L505')
        return "default"


def output_dir(profile_id=""):
    p = os.path.join(storage_root(profile_id), "output")
    os.makedirs(p, exist_ok=True)
    return p


def tmp_dir(profile_id=""):
    p = os.path.join(storage_root(profile_id), "tmp")
    os.makedirs(p, exist_ok=True)
    return p


def db_path(profile_id=""):
    d = storage_root(profile_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "genlab.db")


def log_path():
    try:
        from config_module import _root
        logs = os.path.join(_root(), "logs")
    except Exception:
        swallow.note('config.log_path:L532')
        logs = os.path.join(app_root(), "logs")
    os.makedirs(logs, exist_ok=True)
    return os.path.join(logs, "comfyui.log")


def ensure_dirs():
    """Создать рабочие каталоги модуля (вызывается на старте сервера)."""
    for fn in (output_dir, tmp_dir, db_path):
        try:
            fn()
        except Exception as e:
            _logger().warning(f"[genlab] не удалось создать каталог: {e}")