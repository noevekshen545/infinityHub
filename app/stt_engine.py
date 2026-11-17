"""
STT Engine — локальное распознавание речи (faster-whisper) для русского.

Endpoints (используются server.py):
  STT.transcribe(audio_path, language="ru") -> {text, segments, words}
  STT.status() -> {available, model, loaded, error}
  STT.load(model="turbo", language="ru", device=None) -> bool

Модель скачивается в models/whisper/ и кешируется huggingface_hub.
По умолчанию: faster-whisper-large-v3-turbo (~1.5 ГБ, лучшее качество для русского).
Устройство определяется автоматически: CUDA (float16) при наличии, иначе CPU (int8).
"""
import os, json, logging, threading
from typing import Optional
import swallow

logger = logging.getLogger("infinity")

BASE = os.path.dirname(os.path.abspath(__file__))

def _auto_download_allowed() -> bool:
    """Разрешена ли автозагрузка модели распознавания из сети.

    По умолчанию — нет: переносимая сборка должна работать офлайн, а первый
    запуск не должен молча тянуть полтора гигабайта. Включается настройкой
    speech.stt.auto_download = true или переменной окружения.
    """
    if os.environ.get("INFINITY_STT_NO_DOWNLOAD", "").strip() not in ("", "0", "false"):
        return False
    try:
        from config_module import get_speech
        stt = get_speech().get("stt") or {}
        return bool(stt.get("auto_download", False))
    except Exception:
        swallow.note('stt_engine._auto_download_allowed:L34')
        return False


def _resolve_models_dir() -> str:
    """Каталог моделей whisper: env INFINITY_STT_MODELS_DIR →
    config speech.stt.models_dir → <проект>/models/whisper.

    Если модели лежат на медленном маунте (например /mnt/c — NTFS через WSL),
    загрузка turbo (~1.5ГБ) занимает десятки секунд; укажите путь на нативной
    ФС (например ~/.infinity/models/whisper) — загрузка ускорится в разы.
    """
    d = os.environ.get("INFINITY_STT_MODELS_DIR")
    if d:
        return os.path.expanduser(d)
    try:
        from config_module import get_speech, _expand_path
        cfg_d = ((get_speech() or {}).get("stt") or {}).get("models_dir")
        if cfg_d:
            # _expand_path разворачивает ${ROOT} и ~ — без него остаётся
            # неразвёрнутый ${ROOT}/models/whisper и каталог не находится.
            return os.path.expanduser(str(_expand_path(str(cfg_d))))
    except Exception:
        swallow.note('stt_engine._resolve_models_dir:L57')
        pass
    return os.path.join(BASE, "models", "whisper")

MODELS_DIR = _resolve_models_dir()
os.makedirs(MODELS_DIR, exist_ok=True)
try:
    import platform
    if os.path.isdir("/mnt/c") and MODELS_DIR.startswith("/mnt/"):
        logger.warning(f"[STT] Модели на медленном маунте: {MODELS_DIR} "
                       f"(платформа {platform.system()}). Задайте "
                       f"INFINITY_STT_MODELS_DIR или speech.stt.models_dir "
                       f"на нативной ФС для быстрой загрузки.")
except Exception:
    swallow.note('stt_engine.<module>:L71')
    pass

_MODEL = None
_MODEL_NAME = None
_MODEL_LANG = "ru"
_MODEL_DEVICE = "cpu"
_MODEL_COMPUTE = "int8"
_LOAD_LOCK = threading.Lock()
_AVAILABLE = None  # None = not yet checked
_LOAD_ERROR = None


def _patch_av_open_compat():
    """Совместимость av.open с faster-whisper.

    faster-whisper вызывает av.open(..., metadata_errors="ignore"), но
    установленная версия PyAV такого параметра не знает — распознавание
    падало с TypeError до того, как модель вообще что-то услышала.
    Молча отбрасываем неизвестный аргумент.
    """
    try:
        import inspect
        import av
        try:
            params = inspect.signature(av.open).parameters
            if "metadata_errors" in params:
                return
        except (TypeError, ValueError):
            swallow.note('stt_engine._patch_av_open_compat:L100')
            pass  # встроенная функция — сигнатуры нет, проверяем вызовом ниже
        orig = av.open

        def _open(file, *args, **kwargs):
            kwargs.pop("metadata_errors", None)
            return orig(file, *args, **kwargs)

        av.open = _open
        logger.info("[STT] av.open: аргумент metadata_errors не поддерживается, отброшен")
    except Exception as e:
        logger.debug(f"[STT] патч av.open не понадобился/не удался: {e}")


# faster-whisper принимает только короткие коды ISO-639-1 («ru», «en»),
# а браузер и часть настроек присылают региональный («ru-RU», «en_US»).
# Без нормализации модель падала: "'ru-RU' is not a valid language code".
_WHISPER_LANGS = frozenset("""
af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr
gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt
lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk
sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh yue
""".split())

# Синонимы, которые whisper не знает, но которые встречаются в конфигах/браузере
_LANG_ALIASES = {
    "iw": "he", "in": "id", "ji": "yi", "jw": "jw", "mo": "ro", "ua": "uk",
    "pt_br": "pt", "zh_cn": "zh", "zh_tw": "zh", "nb": "no", "nn_no": "nn",
}


def normalize_lang(lang) -> Optional[str]:
    """Привести код языка к тому, что понимает faster-whisper.

    «ru-RU»/«ru_RU»/«RU» → «ru»; неизвестный код → None (автоопределение),
    чтобы не падать с ошибкой на любом мусорном значении из настроек.
    """
    if not lang:
        return None
    raw = str(lang).strip().lower().replace("_", "-")
    if not raw or raw in ("auto", "any", "none"):
        return None
    raw = _LANG_ALIASES.get(raw.replace("-", "_"), raw)
    base = raw.split("-", 1)[0].strip()
    if base in _WHISPER_LANGS:
        return base
    if raw in _WHISPER_LANGS:
        return raw
    logger.warning("[STT] неизвестный код языка %r — определяем автоматически", lang)
    return None


def is_available() -> bool:
    """Проверить, установлен ли faster-whisper."""
    global _AVAILABLE, _LOAD_ERROR
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        _patch_av_open_compat()
        _AVAILABLE = True
    except Exception as e:
        _AVAILABLE = False
        _LOAD_ERROR = str(e)
        logger.warning(f"[STT] faster-whisper not available: {e}")
    return _AVAILABLE


def status() -> dict:
    try:
        threads = _cpu_threads()
    except Exception:
        swallow.note('stt_engine.status:L172')
        threads = 0
    return {
        "available": is_available(),
        "loaded": _MODEL is not None,
        "model": _MODEL_NAME,
        "language": _MODEL_LANG,
        "device": _MODEL_DEVICE,
        "compute_type": _MODEL_COMPUTE,
        "cpu_threads": threads,
        "models_dir": MODELS_DIR,
        "error": _LOAD_ERROR,
    }


PREFERRED_MODELS = ("large-v3-turbo", "turbo", "small", "medium", "base", "tiny")

# Профиль «качество/скорость» -> модель. Замер на Xeon E5-2696 v4 (только AVX2,
# без CUDA) на фразе 3.4 с, int8, 16 потоков:
#   base  — 0.73 с, средний WER 21% на чистом, 43% при SNR 6 дБ
#   small — 2.2 с,  19% / 36%
#   turbo — 7.6 с,  18% / 21%, то есть МЕДЛЕННЕЕ реального времени
# faster-whisper всегда паддит вход до 30-секундного окна энкодера, поэтому
# тяжёлая модель платит полную стоимость даже за короткое слово. На CPU дефолтом
# должен быть base; turbo имеет смысл только с GPU.
PROFILE_MODELS = {"fast": "base", "balanced": "small", "accurate": "turbo"}


def resolve_model(model: str = None, device: str = "auto") -> str:
    """Какую модель грузить: запрошенную, из настроек, по профилю или лучшую.

    Порядок: аргумент → ``speech.stt.local_model`` → ``speech.stt.profile`` →
    лучшая из локально доступных. Пустая строка означает «реши сам»: на CPU это
    base, на CUDA — turbo. Раньше выбор модели из настроек ни на что не влиял,
    а движок всегда брал turbo, из-за чего фраза в 3 секунды читалась 16 секунд.
    """
    want = str(model or "").strip()
    try:
        from config_module import get_speech
        stt = (get_speech() or {}).get("stt") or {}
    except Exception:
        swallow.note('stt_engine.resolve_model:L213')
        stt = {}
    if not want:
        want = str(stt.get("local_model") or "").strip()
    if want:
        return want
    local = _iter_local_models()
    profile = str(stt.get("profile") or "").strip().lower()
    if device == "cuda":
        # С видеокартой можно позволить себе тяжёлую модель.
        candidates = [profile or "accurate", "balanced", "fast"]
    else:
        candidates = [profile or "fast", "balanced", "fast"]
    for key in candidates:
        name = PROFILE_MODELS.get(key)
        if not name:
            continue
        # 'turbo' и 'large-v3-turbo' — одна модель под двумя именами, поэтому
        # проверяем оба и берём то, под которым веса реально лежат.
        aliases = ("turbo", "large-v3-turbo") if name == "turbo" else (name,)
        for alias in aliases:
            if alias in local:
                return alias
    for name in PREFERRED_MODELS:
        if name in local:
            return name
    return "turbo"


class _CudaUnavailable(RuntimeError):
    """Ошибка о том, что CUDA-библиотеки (cuBLAS/cuDNN) недоступны."""


_CUDA_LIB_MARKERS = ("libcublas", "cublas", "libcudnn", "cudnn", "libnvrtc",
                     "cuda runtime", "cuda driver")


def _is_cuda_lib_error(exc) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _CUDA_LIB_MARKERS)


def _raise_cuda(exc):
    """Прокинуть CUDA-ошибку как _CudaUnavailable, остальные — как есть."""
    if _is_cuda_lib_error(exc):
        raise _CudaUnavailable(str(exc)) from exc
    raise exc


def _config_stt_settings():
    """Настройки STT из config.json (speech.stt.local_device / local_compute_type)."""
    try:
        from config_module import get_speech
        stt = (get_speech() or {}).get("stt") or {}
        device = str(stt.get("local_device") or "auto").strip().lower()
        compute = str(stt.get("local_compute_type") or "auto").strip().lower()
        return (device if device in ("cpu", "cuda") else "auto",
                compute if compute in ("int8", "float16", "int8_float16") else "auto")
    except Exception:
        swallow.note('stt_engine._config_stt_settings:L272')
        return "auto", "auto"


def _cpu_threads() -> int:
    """Сколько потоков отдать Whisper на CPU.

    ctranslate2 при ``cpu_threads=0`` (значение по умолчанию) фактически
    задействует четыре потока, а не все ядра: на замере фразы длиной 3.4 с
    turbo считал её 15.9 с, а с 16 потоками — 7.6 с. Отдаём половину ядер и не
    больше 16: дальше растёт только конкуренция с LLM, который на этой же
    машине занимает те же ядра (замер на 44 потоках был хуже, чем на 16).

    Переопределение — ``INFINITY_STT_THREADS`` в окружении или
    ``speech.stt.local_threads`` в config.json.
    """
    raw = os.environ.get("INFINITY_STT_THREADS", "").strip()
    if not raw.isdigit():
        try:
            from config_module import get_speech
            raw = str(((get_speech() or {}).get("stt") or {}).get("local_threads") or "").strip()
        except Exception:
            swallow.note('stt_engine._cpu_threads:L294')
            raw = ""
    # 0 = авто (как в конфиге, так и в переменной окружения).
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, min(16, (os.cpu_count() or 4) // 2))


def _pick_device():
    """Автоопределение устройства: CUDA если доступна, иначе CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        swallow.note('stt_engine._pick_device:L309')
        pass
    return "cpu"


def _compute_for_device(device):
    return "float16" if device == "cuda" else "int8"


def _iter_local_models() -> dict:
    """Найти локально скачанные модели faster-whisper.

    Возвращает {имя_модели: путь_к_папке}. Поддерживаются оба формата:
      * models/whisper/<имя>/model.bin        (snapshot_download с local_dir)
      * models/whisper/models--<org>--<name>/snapshots/<hash>/model.bin (HF cache)
    """
    found = {}
    try:
        entries = os.listdir(MODELS_DIR)
    except OSError:
        swallow.note('stt_engine._iter_local_models:L329')
        return found

    # Прямые папки models/whisper/<имя>/
    # Прямые папки: models/<имя>/ и models/whisper/<имя>/.
    # Раньше проверялся только models/<имя>/, хотя раскладка в проекте —
    # models/whisper/<имя>/, из-за чего локальные модели не находились
    # и приложение уходило в автозагрузку с HuggingFace.
    for _base in (MODELS_DIR, os.path.join(MODELS_DIR, "whisper")):
        for name in PREFERRED_MODELS:
            d = os.path.join(_base, name)
            if os.path.exists(os.path.join(d, "model.bin")):
                found.setdefault(name, d)

    # HF cache: models--org--name
    for sub in entries:
        if not sub.startswith("models--"):
            continue
        repo = sub[len("models--"):].replace("--", "/").lower()
        snap = os.path.join(MODELS_DIR, sub, "snapshots")
        if not os.path.isdir(snap):
            continue
        try:
            hashes = os.listdir(snap)
        except OSError:
            swallow.note('stt_engine._iter_local_models:L354')
            continue
        for h in hashes:
            p = os.path.join(snap, h, "model.bin")
            if not os.path.exists(p):
                continue
            for name in PREFERRED_MODELS:
                if name in repo:
                    found.setdefault(name, os.path.join(snap, h))
    return found


def _resolve_local_dir(model: str):
    """Путь к локальной папке модели (или None)."""
    d = os.path.join(MODELS_DIR, model)
    if os.path.exists(os.path.join(d, "model.bin")):
        return d
    for name, path in _iter_local_models().items():
        if name == model or (model == "turbo" and name == "large-v3-turbo") \
                or (model == "large-v3-turbo" and name == "turbo"):
            return path
    return None


def load(model: str = None, language: str = "ru",
         device: str = None, compute_type: str = None) -> bool:
    """
    Загрузить (или переключить) модель.
    Доступные модели: tiny (~75 МБ), base (~140 МБ), small (~460 МБ), medium (~1.5 ГБ),
    large-v3 (~3 ГБ), large-v3-turbo (~1.5 ГБ, быстрее).
    Для русского языка рекомендуются модели с поддержкой multilingual (base, small, medium, large-v3, large-v3-turbo).
    model=None: выбрать по профилю из config.json (speech.stt.profile) и устройству —
    на CPU по умолчанию base, на CUDA turbo; см. resolve_model.
    device/compute_type: если не указаны (или 'auto') — CUDA (float16) при наличии, иначе CPU (int8).
    Если модель уже есть локально в models/whisper/, она используется без скачивания.
    """
    global _MODEL, _MODEL_NAME, _MODEL_LANG, _MODEL_DEVICE, _MODEL_COMPUTE, _LOAD_ERROR
    if not is_available():
        return False
    cfg_device, cfg_compute = _config_stt_settings()
    if not device or device == "auto":
        device = cfg_device if cfg_device != "auto" else _pick_device()
    if not compute_type or compute_type == "auto":
        compute_type = (cfg_compute if cfg_compute != "auto"
                        else _compute_for_device(device))
    if device not in ("cpu", "cuda"):
        device = "cpu"
    # Модель выбираем после устройства: профиль зависит от того, есть ли GPU.
    model = resolve_model(model, device)
    with _LOAD_LOCK:
        try:
            return _load_inner(model, language, device, compute_type, _retry_cpu=True)
        except Exception as e:
            _LOAD_ERROR = str(e)
            logger.error(f"[STT] load error: {e}", exc_info=True)
            return False


def _load_inner(model: str, language: str, device: str, compute_type: str,
                _retry_cpu: bool = False) -> bool:
    """Загрузка модели с автоматическим фолбэком на CPU при неисправном CUDA."""
    global _MODEL, _MODEL_NAME, _MODEL_LANG, _MODEL_DEVICE, _MODEL_COMPUTE, _LOAD_ERROR
    try:
        from faster_whisper import WhisperModel
        os.environ.setdefault("HF_HOME", MODELS_DIR)
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", MODELS_DIR)
        logger.info(f"[STT] loading model={model} lang={language} device={device} compute={compute_type}")
        local_dir = _resolve_local_dir(model)
        # Потоки задаются только для CPU: на CUDA CTranslate2 считает их сам, и
        # переданное значение там просто игнорируется.
        threads = _cpu_threads() if device == "cpu" else 0
        if local_dir:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            logger.info(f"[STT] using local model from {local_dir}")
            _MODEL = WhisperModel(local_dir, device=device, compute_type=compute_type,
                                  cpu_threads=threads, local_files_only=True)
        else:
            # Автозагрузка с Hugging Face. В переносимой сборке её лучше
            # отключить (config.json → speech.stt.auto_download = false или
            # env INFINITY_STT_NO_DOWNLOAD=1): иначе первый запуск молча
            # тянет из сети полтора гигабайта.
            if _auto_download_allowed():
                logger.info(f"[STT] local model not found, downloading {model} from Hub...")
                _MODEL = WhisperModel(model, device=device, compute_type=compute_type,
                                      cpu_threads=threads, download_root=MODELS_DIR)
            else:
                raise RuntimeError(
                    f"Модель распознавания '{model}' не найдена в {MODELS_DIR} "
                    f"и автозагрузка отключена. Положите модель вручную "
                    f"(например models\\stt\\{model}) или включите "
                    f"speech.stt.auto_download в config.json.")
        _MODEL_NAME = model
        _MODEL_LANG = normalize_lang(language) or language
        _MODEL_DEVICE = device
        _MODEL_COMPUTE = compute_type
        _LOAD_ERROR = None
        logger.info(f"[STT] model loaded: {_MODEL_NAME} ({device}/{compute_type})")
        return True
    except Exception as e:
        if _is_cuda_lib_error(e) and device == "cuda" and _retry_cpu:
            logger.warning(f"[STT] CUDA недоступен ({e}), перезагрузка на CPU (int8)...")
            _LOAD_ERROR = (f"CUDA недоступен ({e}); работает на CPU (int8)")
            return _load_inner(model, language, "cpu", "int8", _retry_cpu=False)
        _MODEL = None
        _LOAD_ERROR = str(e)
        raise


def _ensure_loaded():
    """Ленивая автозагрузка модели при первом обращении (профиль по устройству)."""
    global _MODEL
    if _MODEL is None:
        local = _iter_local_models()
        device = _pick_device()
        wanted = resolve_model(None, device)
        order = [wanted] + [n for n in PREFERRED_MODELS if n != wanted]
        for name in order:
            if name in local and load(name, "ru", device=device):
                return True
        if load(wanted, "ru", device=device):
            return True
    return _MODEL is not None


def ensure_loaded():
    """Публичная обёртка над _ensure_loaded (для wakeword_engine и пр.)."""
    return _ensure_loaded()


# ---- быстрый режим для коротких окон (wake-word) --------------------------
# Детектору слова пробуждения нельзя занимать основную модель: он вызывается
# несколько раз в секунду, а модель small на CPU тратит ~2 с на фразу. Держим
# отдельную лёгкую модель (base/tiny), грузим её один раз и декодируем жадно.
_SHORT_MODEL = None
_SHORT_NAME = None
_SHORT_LOCK = threading.Lock()


def _short_threads() -> int:
    """Немного потоков: wake-word не должен конкурировать с основным STT и LLM.

    Замер (base, int8, окно 0.6 с, 44 ядра): 4 потока — 1.06 с, 8 — 0.72 с.
    Больше 8 не даёт выигрыша, но забирает ядра у LLM.
    """
    raw = os.environ.get("INFINITY_WAKEWORD_STT_THREADS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, min(8, (os.cpu_count() or 4) // 4))


def _short_model_candidates():
    env = os.environ.get("INFINITY_WAKEWORD_STT_MODEL", "").strip()
    if env:
        return [env]
    local = _iter_local_models()
    return [n for n in ("base", "tiny") if n in local]


def ensure_short_model() -> bool:
    """Загрузить лёгкую модель для распознавания коротких окон."""
    global _SHORT_MODEL, _SHORT_NAME
    if _SHORT_MODEL is not None:
        return True
    if not is_available():
        return False
    with _SHORT_LOCK:
        if _SHORT_MODEL is not None:
            return True
        for name in _short_model_candidates():
            local_dir = _resolve_local_dir(name)
            if not local_dir:
                continue
            try:
                from faster_whisper import WhisperModel
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                model = WhisperModel(local_dir, device="cpu", compute_type="int8",
                                     cpu_threads=_short_threads(), local_files_only=True)
                _SHORT_MODEL = model
                _SHORT_NAME = name
                logger.info(f"[STT] wake-word model loaded: {name} "
                            f"(cpu/int8, {_short_threads()} threads)")
                return True
            except Exception as e:
                logger.warning(f"[STT] wake-word model {name} load failed: {e}")
    return False


def transcribe_short(audio_path: str, language: str = "ru") -> dict:
    """Распознать короткое окно быстро: жадное декодирование, без VAD.

    Если лёгкой модели нет (не скачана), работаем основной моделью без VAD —
    медленнее, но детекция не отваливается.
    """
    if not is_available():
        return {"text": "", "error": "faster-whisper not installed"}
    if not os.path.exists(audio_path):
        return {"text": "", "error": f"File not found: {audio_path}"}
    if not ensure_short_model():
        return transcribe(audio_path, language=language, vad_filter=False,
                          no_speech_threshold=0.6, word_timestamps=False)
    try:
        lang = normalize_lang(language) or "ru"
        segments, info = _SHORT_MODEL.transcribe(
            audio_path,
            language=lang,
            beam_size=1,
            best_of=1,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
            temperature=0.0,
        )
        text = " ".join((s.text or "").strip() for s in segments).strip()
        return {"text": text, "language": getattr(info, "language", lang),
                "model": _SHORT_NAME}
    except Exception as e:
        logger.warning(f"[STT] transcribe_short error: {e}")
        return {"text": "", "error": str(e)}


def ensure_model(model: str = None, language: str = "ru") -> bool:
    """Гарантировать загрузку модели. Пустая модель = выбрать по профилю.

    Если уже загружена другая модель — переключиться на запрошенную.
    При неудаче загрузки — мягкий фолбэк на лучшую локальную модель.
    """
    device = _pick_device()
    want = resolve_model(model, device)
    if _MODEL is not None:
        loaded = _MODEL_NAME or ""
        if loaded == want or (want == "turbo" and loaded == "large-v3-turbo") \
                or (want == "large-v3-turbo" and loaded == "turbo"):
            return True
    if load(want, language, device=device):
        return True
    return _ensure_loaded()


def set_vad_parameters(threshold: float = 0.5, min_speech_duration_ms: int = 250, max_speech_duration_s: float = 30.0,
                       min_silence_duration_ms: int = 1000):
    """Настроить параметры VAD для лучшего распознавания речь/паузы."""
    global _VAD_PARAMS
    _VAD_PARAMS = {
        "threshold": threshold,
        "min_speech_duration_ms": min_speech_duration_ms,
        "max_speech_duration_s": max_speech_duration_s,
        "min_silence_duration_ms": min_silence_duration_ms,
    }
    logger.info(f"[STT] VAD params updated: {_VAD_PARAMS}")


_VAD_PARAMS = {
    "threshold": 0.3,  # Lower threshold = more sensitive (captures quieter speech)
    "min_speech_duration_ms": 100,  # Shorter min speech = captures sentence starts
    "max_speech_duration_s": 30.0,
    "min_silence_duration_ms": 800,  # Shorter silence = keeps segments tighter
}


def transcribe(audio_path: str, language: Optional[str] = None,
               word_timestamps: bool = False,
               vad_filter: bool = True,
               no_speech_threshold: Optional[float] = 0.3) -> dict:
    """
    Транскрибировать аудиофайл.
    Возвращает формат, совместимый с OpenAI verbose_json:
      {
        "text": "полный текст",
        "language": "ru",
        "segments": [{"id":0,"start":0,"end":1.2,"text":"Привет","words":[...]}],
        "words": [{start,end,word,probability}]
      }
    word_timestamps по умолчанию выключены: выравнивание по словам стоит 10-13%
    времени декодирования, а ни один вызывающий в приложении эти поля не читает.
    vad_filter=False — полное распознавание без VAD (музыка/длинные файлы):
    окна не выбрасываются, включается температурный fallback.
    """
    if not is_available():
        return {"error": "faster-whisper not installed. pip install faster-whisper", "text": ""}
    if not os.path.exists(audio_path):
        return {"error": f"File not found: {audio_path}", "text": ""}
    if not _ensure_loaded():
        return {"error": _LOAD_ERROR or "Model load failed", "text": ""}
    try:
        lang = language or _MODEL_LANG
        return _run_transcription(audio_path, lang, word_timestamps,
                                  vad_filter, no_speech_threshold)
    except _CudaUnavailable as e:
        # CUDA-библиотеки (cuBLAS/cuDNN) не загрузились в рантайме — перезапуск на CPU
        logger.warning(f"[STT] CUDA сбой при распознавании ({e}); "
                       f"перезагрузка на CPU (int8) и повторная попытка...")
        global _MODEL
        _MODEL = None
        if not load(_MODEL_NAME, _MODEL_LANG, device="cpu", compute_type="int8"):
            logger.error(f"[STT] reload on CPU failed: {_LOAD_ERROR}")
            return {"error": _LOAD_ERROR or "Model reload failed", "text": ""}
        try:
            logger.info(f"[STT] retrying transcription on CPU ({_MODEL_DEVICE}/{_MODEL_COMPUTE})")
            return _run_transcription(audio_path, lang, word_timestamps,
                                      vad_filter, no_speech_threshold)
        except Exception as e2:
            logger.error(f"[STT] transcribe retry error: {e2}", exc_info=True)
            return {"error": str(e2), "text": ""}
    except Exception as e:
        logger.error(f"[STT] transcribe error: {e}", exc_info=True)
        return {"error": str(e), "text": ""}


def _run_transcription(audio_path: str, lang: str, word_timestamps: bool,
                       vad_filter: bool = True,
                       no_speech_threshold: Optional[float] = 0.3) -> dict:
    """Выполнить транскрипцию (внутренняя функция, кидает исключения наружу)."""
    lang = normalize_lang(lang)          # «ru-RU» → «ru», мусор → автоопределение
    try:
        # Без VAD — декодируем все 30-секундные окна целиком,
        # с температурным fallback для сложных фрагментов (музыка, шум).
        if vad_filter:
            temperature = 0.0
        else:
            temperature = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
            no_speech_threshold = None
        segments_iter, info = _MODEL.transcribe(
            audio_path,
            language=lang,
            beam_size=5,
            best_of=5,
            vad_filter=vad_filter,
            vad_parameters=_VAD_PARAMS,
            word_timestamps=word_timestamps,
            condition_on_previous_text=True,
            temperature=temperature,
            no_speech_threshold=no_speech_threshold,
            compression_ratio_threshold=2.0,
            log_prob_threshold=-1.0,
        )
        segments = []
        all_words = []
        full_text_parts = []
        for i, seg in enumerate(segments_iter):
            words = []
            if seg.words:
                for w in seg.words:
                    wd = {
                        "start": float(w.start) if w.start is not None else 0.0,
                        "end": float(w.end) if w.end is not None else 0.0,
                        "word": w.word or "",
                        "probability": float(w.probability) if w.probability is not None else 0.0,
                    }
                    words.append(wd)
                    all_words.append(wd)
            seg_d = {
                "id": i,
                "start": float(seg.start) if seg.start is not None else 0.0,
                "end": float(seg.end) if seg.end is not None else 0.0,
                "text": (seg.text or "").strip(),
                "words": words,
            }
            segments.append(seg_d)
            full_text_parts.append(seg.text or "")
        full_text = " ".join(s.strip() for s in full_text_parts if s).strip()
        logger.info(f"[STT] transcribed lang={info.language if info else lang} "
                    f"dur={info.duration if info else 0:.1f}s text='{full_text[:80]}'")
        return {
            "text": full_text,
            "language": info.language if info else lang,
            "duration": info.duration if info else 0,
            "segments": segments,
            "words": all_words,
        }
    except Exception as e:
        _raise_cuda(e)
        raise


def transcribe_bytes(audio_bytes: bytes, language: Optional[str] = None,
                     suffix: str = ".webm",
                     vad_filter: bool = True,
                     no_speech_threshold: Optional[float] = 0.3,
                     word_timestamps: bool = False) -> dict:
    """Сохранить байты во временный файл и транскрибировать."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp = f.name
    try:
        return transcribe(tmp, language=language, vad_filter=vad_filter,
                          no_speech_threshold=no_speech_threshold,
                          word_timestamps=word_timestamps)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            swallow.note('stt_engine.transcribe_bytes:L748')
            pass
