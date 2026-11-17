"""Секция 'ЛОГИРОВАНИЕ С РОТАЦИЕЙ' монолита `app/server_backup.py`.

Вынесена механически (`tools/_extract_section.py`): код ниже не правился,
имена из других секций монолита не импортируются, а связываются
хвостом монолита через `_bind()` (см. `_FOREIGN`): порядок хвостовых
ре-экспортов произвольный, и прямой импорт упёрся бы в частично
инициализированный модуль.
Монолит ре-экспортирует всё, что определено здесь, поэтому прежние
потребители (`import server`, `import server_backup`) работают без правок.
"""

# перенесено из головы монолита: эти имена секция использовала как импорты
import os, sys, json, math, argparse, time, re, io, zipfile, tempfile, base64, hashlib, logging, subprocess, select, mimetypes, configparser, shlex
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, quote
import urllib.request
import urllib.error
from io import BytesIO
import threading
from config_module import (get_config, get_api_key, load_config, reload_config,
                           normalize_openai_base)
import ask_modes

# эти имена нужны операторам верхнего уровня секции, поэтому импортируются
# сразу: все они определены в монолите ДО хвостовых ре-экспортов
from server_backup import (
    AI_DIR,
    ATTACHMENTS_DIR,
    CFG,
    CHAT_DIR,
    MEMORY_DIR,
    NOTES_DIR,
    PLUGINS_DIR,
)
# ===================== ЛОГИРОВАНИЕ С РОТАЦИЕЙ =====================

# `__file__` в этом коде означал файл монолита: из него считались пути
# (templates/, models/, каталоги профиля). После выноса он указал бы на
# app/legacy/..., поэтому возвращаем исходное значение.
import os as _path_anchor_os
import swallow
__file__ = _path_anchor_os.path.join(
    _path_anchor_os.path.dirname(_path_anchor_os.path.dirname(
        _path_anchor_os.path.abspath(__file__))), 'server_backup.py')
del _path_anchor_os

_LOG_CFG = CFG.get("logging", {})
_log_level = getattr(logging, _LOG_CFG.get("level", "INFO").upper(), logging.INFO)
_log_file = os.path.expanduser(_LOG_CFG.get("file", "~/.infinity/server.log"))
_log_max_bytes = _LOG_CFG.get("max_bytes", 10485760)
_log_backup_count = _LOG_CFG.get("backup_count", 5)

try:
    os.makedirs(os.path.dirname(_log_file), exist_ok=True)
except PermissionError:
    swallow.note('rotating_logging.<module>:L57')
    _log_file = "/tmp/infinity_server.log"
    os.makedirs("/tmp", exist_ok=True)

logger = logging.getLogger("infinity")
logger.setLevel(_log_level)

# Файловый обработчик с ротацией
_file_handler = RotatingFileHandler(
    _log_file,
    maxBytes=_log_max_bytes,
    backupCount=_log_backup_count,
    encoding="utf-8"
)
_file_handler.setLevel(_log_level)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(_file_handler)

# Консольный обработчик
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
logger.addHandler(_console_handler)
# Не прокидывать записи в root-логгер: иначе модули с logging.basicConfig()
# (например desktop_api) дублируют каждую строку в консоль — двойной
# консольный I/O заметно тормозит сервер под WSL/Windows.
logger.propagate = False
# Маркер сборки: по этой строке в логе видно, какой именно файл загрузился
# (пакет server/ реэкспортирует server_backup — важно видеть источник).
try:
    logger.info(f"server_backup.py loaded: file={os.path.abspath(__file__)}, "
                f"mtime={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(__file__)))}")
except Exception:
    swallow.note('rotating_logging.<module>:L93')
    pass


# LLM Backend config
LLM_BACKEND = CFG.get("backend", "lmstudio")
_LM_CFG = CFG.get("lm_studio", {})
LM_STUDIO_BASE_URL = _LM_CFG.get("url", "http://localhost:1234").strip()
LM_STUDIO_URL = LM_STUDIO_BASE_URL + "/v1/chat/completions"
LM_STUDIO_MODEL = _LM_CFG.get("model", "gemma-3-1b-it@q8_0")
LM_STUDIO_EMBED_MODEL = _LM_CFG.get("embed_model", "nomic-ai/nomic-embed-text-v1.5-GGUF")
LM_STUDIO_API_KEY = get_api_key(CFG)
_LLAMA_CPP_CFG = CFG.get("llama_cpp", {})
LLAMA_CPP_BASE_URL = _LLAMA_CPP_CFG.get("url", "http://localhost:8080").strip()
LLAMA_CPP_URL = LLAMA_CPP_BASE_URL + "/v1/chat/completions"
LLAMA_CPP_MODEL = _LLAMA_CPP_CFG.get("model", "gemma-3-1b-it@q8_0")
LLAMA_CPP_API_KEY = _LLAMA_CPP_CFG.get("api_key", "")
# Пользовательский облачный бэкенд с OpenAI-совместимым API.
# Раньше поддержка была добавлена только в app/server.py — мёртвый файл,
# который не импортируется (его перекрывает пакет app/server/). Поэтому
# настройка «Облачный / свой» не влияла на работу и билдер расширений
# уходил в LM Studio вместо активного движка.
_CUSTOM_CFG = CFG.get("custom", {})
CUSTOM_BASE_URL = normalize_openai_base(_CUSTOM_CFG.get("url", ""))
CUSTOM_URL = CUSTOM_BASE_URL + "/v1/chat/completions" if CUSTOM_BASE_URL else ""
CUSTOM_MODEL = _CUSTOM_CFG.get("model", "")
CUSTOM_API_KEY = _CUSTOM_CFG.get("api_key", "")

# Piper TTS (локальный качественный синтез речи)
PIPER_TTS_AVAILABLE = False
PIPER_VOICE = None
_PIPER_VOICE_CACHE = {}  # voice_id -> PiperVoice
_PIPER_VOICE_CACHE_LOCK = threading.Lock()
_PIPER_FAILED = set()    # пути моделей, которые не удалось загрузить (не повторяем)
_PIPER_LOADED_PATH = None  # какая модель реально загружена (может отличаться от дефолта)

def _get_piper_model_dir():
    """Путь к моделям Piper (поддержка PyInstaller)"""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        _portable = os.path.join(os.environ.get("INFINITY_ROOT", ""), "models", "piper")
        if os.path.isdir(_portable):
            return _portable
        return os.path.join(sys._MEIPASS, "models", "piper")
    else:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "piper")

def _get_piper_cache_dir():
    """Системный кэш Piper (для обратной совместимости)"""
    return os.path.expanduser("~/.infinity/piper_models")



def _find_piper_model(voice_id=None):
    """Ищем модель Piper: сначала в папке проекта, затем в системном кэше.

    voice_id: имя файла без расширения (например, "ru_RU-ruslan-medium").
              Если None — берётся первая найденная модель.
    Возвращает (model_path, config_path, source) или (None, None, None).
    """
    project_dir = _get_piper_model_dir()
    cache_dir = _get_piper_cache_dir()

    # Список директорий в порядке приоритета
    dirs = [(project_dir, "project"), (cache_dir, "cache")]

    if voice_id:
        # Ищем конкретную модель
        for d, source in dirs:
            mp_ = os.path.join(d, voice_id + ".onnx")
            cp_ = os.path.join(d, voice_id + ".onnx.json")
            if os.path.exists(mp_):
                return mp_, cp_ if os.path.exists(cp_) else None, source
        return None, None, None

    # Иначе — первую доступную модель (для обратной совместимости)
    for d, source in dirs:
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".onnx") and not fn.endswith(".onnx.json"):
                    mp_ = os.path.join(d, fn)
                    cp_ = os.path.join(d, fn + ".json")
                    if os.path.exists(mp_):
                        return mp_, cp_ if os.path.exists(cp_) else None, source
    return None, None, None

def _list_piper_voices():
    """Возвращает список доступных голосов Piper: [{id, name, source, size_mb}]"""
    voices = []
    seen = set()
    project_dir = _get_piper_model_dir()
    cache_dir = _get_piper_cache_dir()
    for d, source in [(project_dir, "project"), (cache_dir, "cache")]:
        if not os.path.isdir(d):
            continue
        try:
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".onnx") or fn.endswith(".onnx.json"):
                    continue
                voice_id = fn[:-5]  # strip .onnx
                if voice_id in seen:
                    continue
                seen.add(voice_id)
                path = os.path.join(d, fn)
                try:
                    size_mb = round(os.path.getsize(path) / (1024 * 1024), 1)
                except Exception:
                    swallow.note('rotating_logging._list_piper_voices:L196')
                    size_mb = 0
                # Описание голоса — попробуем вытащить из json
                desc = ""
                json_path = os.path.join(d, fn + ".json")
                if os.path.exists(json_path):
                    try:
                        with open(json_path, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
                        # Часто в json есть num_speakers, speaker_id_map, language
                        lang = (cfg.get("language") or {}).get("code_3letters") or cfg.get("language") or ""
                        if isinstance(lang, dict):
                            lang = lang.get("code_3letters") or lang.get("name_native") or ""
                        desc_parts = []
                        if lang:
                            desc_parts.append(str(lang))
                        if cfg.get("num_speakers"):
                            desc_parts.append(f"{cfg['num_speakers']} speaker(s)")
                        if cfg.get("audio", {}).get("sample_rate"):
                            desc_parts.append(f"{cfg['audio']['sample_rate']//1000}kHz")
                        if cfg.get("dataset"):
                            desc_parts.append(str(cfg["dataset"]))
                        desc = " · ".join(desc_parts)
                    except Exception:
                        swallow.note('rotating_logging._list_piper_voices:L220')
                        pass
                voices.append({
                    "id": voice_id,
                    "name": voice_id,
                    "source": source,
                    "size_mb": size_mb,
                    "description": desc,
                })
        except Exception as e:
            logger.warning(f"Piper: не удалось прочитать {d}: {e}")
    return voices

def _tts_publish():
    """Зеркалировать состояние TTS, если `_publish_tts_state` уже объявлена.

    Нужна именно проверка: `_get_piper_voice()` вызывается из блока инициализации
    Piper в теле модуля, который исполняется раньше, чем определён паблинг.
    """
    _f = globals().get("_publish_tts_state")
    if _f is not None:
        try:
            _f()
        except Exception:
            swallow.note('rotating_logging._tts_publish:L244')
            pass


def _get_piper_voice(voice_id=None):
    """Получить PiperVoice для указанного voice_id (с кэшированием и откатом).

    Исправление 30.09.2026: раньше битая (например, недокачанная) .onnx
    ломала синтез целиком — даже когда рядом лежал рабочий голос Piper.
    Теперь неудачные модели запоминаются в _PIPER_FAILED, и если запрошенный
    голос не грузится, перебираются остальные доступные.
    """
    global PIPER_VOICE
    global _PIPER_LOADED_PATH
    candidates = []
    model_path, config_path, source = _find_piper_model(voice_id)
    if model_path:
        candidates.append((model_path, config_path))
    # Откат: любой другой доступный голос из каталога. Нужен в обоих случаях —
    # и когда голос задан явно, и когда берётся модель по умолчанию (иначе
    # битая первая модель «по умолчанию» выключала синтез целиком).
    for v in _list_piper_voices():
        mp_, cp_, _src = _find_piper_model(v["id"])
        if mp_ and all(mp_ != c[0] for c in candidates):
            candidates.append((mp_, cp_))

    for mp_, cp_ in candidates:
        if mp_ in _PIPER_FAILED:
            continue
        with _PIPER_VOICE_CACHE_LOCK:
            cached = _PIPER_VOICE_CACHE.get(mp_)
        if cached is not None:
            PIPER_VOICE = cached
            _PIPER_LOADED_PATH = mp_
            _tts_publish()
            return cached
        try:
            from piper import PiperVoice as _PV
            voice = _PV.load(mp_, config_path=cp_)
        except Exception as e:
            _PIPER_FAILED.add(mp_)
            logger.error(f"Piper: модель {os.path.basename(mp_)} не загрузилась ({e})"
                         f" — пробуем другой голос")
            continue
        with _PIPER_VOICE_CACHE_LOCK:
            _PIPER_VOICE_CACHE[mp_] = voice
        PIPER_VOICE = voice  # keep default ref
        _PIPER_LOADED_PATH = mp_
        _tts_publish()
        if voice_id and os.path.basename(mp_) != voice_id + ".onnx":
            logger.warning(f"Piper: голос '{voice_id}' недоступен, используется "
                           f"{os.path.basename(mp_)}")
        elif not voice_id:
            logger.info(f"Piper: модель по умолчанию недоступна, используется "
                        f"{os.path.basename(mp_)}")
        return voice
    return None

try:
    from piper import PiperVoice  # noqa: F401
    _PIPER_MODEL, _PIPER_CONFIG, _PIPER_SOURCE = _find_piper_model()
    if _PIPER_MODEL and os.path.exists(_PIPER_MODEL):
        PIPER_VOICE = _get_piper_voice()
        if PIPER_VOICE is not None:
            PIPER_TTS_AVAILABLE = True
            # ИСПРАВЛЕНИЕ 30.09.2026: логировалась модель по умолчанию, а не та,
            # что реально загрузилась (после отката это разные файлы).
            logger.info(f"Piper TTS загружен: {_PIPER_LOADED_PATH or _PIPER_MODEL}"
                        f" (источник: {_PIPER_SOURCE})")
    else:
        logger.info(f"Piper TTS: модель не найдена. Положите в models/piper/ или ~/.infinity/piper_models/")
except ImportError:
    swallow.note('rotating_logging.<module>:L316')
    pass
except Exception as e:
    logger.warning(f"Piper TTS ошибка: {e}")

# Список силировских спикеров (для UI)
def _list_silero_speakers():
    """Возвращает список доступных спикеров Silero v5_ru."""
    if SILERO_MODEL is None:
        return []
    speakers = list(SILERO_MODEL.speakers or [])
    return speakers

# Markdown / служебные метки, которые ломают Silero (***, ---, ###, ___ и т.п.)
_TTS_JUNK_RE = __import__("re").compile(
    r"(\*{1,3}|_{1,3}|#{1,6}|={1,3}|`{1,3}|~{2,}|\.{3,}|-{2,}|—{2,}|<[^>]+>)"
)

def _sanitize_tts_text(text: str) -> str:
    """Очистка текста для TTS от Markdown-разметки и мусора.

    Silero v5 падает с KeyError на символах вне токенсета (цифры, '-', '()',
    кавычки и т.п.) — путь с акцентами в apply_tts обходит фильтр модели,
    поэтому чистим текст до вызова. Делегируем общему санитайзеру.
    """
    if not text:
        return ""
    from silero_pool import silero_safe_text
    return silero_safe_text(text)


def _apply_silero_rate_pitch(samples, sample_rate, rate=1.0, pitch=0.0):
    """Пост-обработка аудио: rate (скорость) и pitch (высота тона в полутонах).

    - rate=1.0 — без изменений; rate=1.5 — в 1.5 раза быстрее (короче).
    - pitch=0 — без сдвига; pitch=2 — на 2 полутона выше; pitch=-2 — ниже.
    Сначала сдвигаем pitch, потом делаем time-stretch (resample).
    """
    import numpy as np
    if samples is None or len(samples) == 0:
        return samples
    rate = float(rate) if rate else 1.0
    pitch = float(pitch) if pitch else 0.0
    # Защита от мусорных значений
    if rate < 0.25: rate = 0.25
    if rate > 4.0: rate = 4.0
    if pitch < -24: pitch = -24
    if pitch > 24: pitch = 24

    if abs(rate - 1.0) < 0.01 and abs(pitch) < 0.01:
        return samples

    out = np.asarray(samples, dtype=np.float32)

    # 1) Pitch shift (если есть librosa) — передискретизация с коэффициентом 2^(pitch/12)
    if abs(pitch) >= 0.01:
        try:
            import librosa
            out = librosa.effects.pitch_shift(out, sr=sample_rate, n_steps=pitch)
        except ImportError:
            # fallback: грубый pitch shift через resample
            swallow.note('rotating_logging._apply_silero_rate_pitch:L376')
            try:
                factor = 2.0 ** (pitch / 12.0)
                new_len = max(1, int(len(out) / factor))
                idx = np.linspace(0, len(out) - 1, new_len).astype(np.int64)
                shifted = out[idx]
                # возврат к исходной длине
                idx2 = np.linspace(0, len(shifted) - 1, len(out)).astype(np.int64)
                out = shifted[idx2]
            except Exception:
                swallow.note('rotating_logging._apply_silero_rate_pitch:L387')
                pass
        except Exception:
            swallow.note('rotating_logging._apply_silero_rate_pitch:L390')
            pass

    # 2) Time stretch (rate) — ресемплинг: большее значение = короче = быстрее
    if abs(rate - 1.0) >= 0.01:
        try:
            target_len = max(1, int(len(out) / rate))
            idx = np.linspace(0, len(out) - 1, target_len).astype(np.int64)
            out = out[idx]
        except Exception:
            swallow.note('rotating_logging._apply_silero_rate_pitch:L400')
            pass

    # Подрезаем пики после ресемплинга (защита от клиппинга)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 1.0:
        out = out / peak
    return out.astype(np.float32)


def _apply_silero_rate_pitch_to_wav(wav_bytes, rate=1.0, pitch=0.0):
    """Применяет rate/pitch к WAV-байтам (декодирует, обрабатывает, перекодирует)."""
    if abs(float(rate or 1.0) - 1.0) < 0.01 and abs(float(pitch or 0.0)) < 0.01:
        return wav_bytes
    try:
        import io
        import wave
        import numpy as np
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            sw = wf.getsampwidth()
            ch = wf.getnchannels()
            raw = wf.readframes(n)
        if sw != 2 or ch != 1:
            return wav_bytes
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        out = _apply_silero_rate_pitch(samples, sr, rate, pitch)
        audio_int16 = (out * 32767).astype(np.int16)
        out_buf = io.BytesIO()
        with wave.open(out_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio_int16.tobytes())
        return out_buf.getvalue()
    except Exception as e:
        logger.warning(f"_apply_silero_rate_pitch_to_wav failed: {e}")
        return wav_bytes





# Silero TTS v5 (качественная русская модель)
SILERO_TTS_AVAILABLE = False
SILERO_MODEL = None
SILERO_MODEL_VERSION = "v5_5_ru"  # по умолчанию — самая свежая
SILERO_SAMPLE_RATE = 48000  # Silero v5 использует 48kHz

def _get_silero_hub_dir():
    """Определяем путь к Silero hub кэшу (PyInstaller / переносимая сборка)."""
    # При сборке PyInstaller модель будет в _MEIPASS
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        _portable = os.path.join(os.environ.get("INFINITY_ROOT", ""),
                                 "models", "silero", "silero_hub")
        if os.path.isdir(_portable):
            return _portable
        return os.path.join(sys._MEIPASS, "models", "silero", "silero_hub")
    # Переносимая сборка: модель лежит в <корень>/models/silero/silero_hub,
    # а не в app/models — иначе на флешке её не будет, и синтез молча
    # не найдёт кэш и уйдёт в браузерный webspeech.
    try:
        from config_module import _root
        _portable = os.path.join(_root(), "models", "silero", "silero_hub")
        if os.path.isdir(_portable):
            return _portable
    except Exception:
        swallow.note('rotating_logging._get_silero_hub_dir:L465')
        pass
    # Обычный режим — модель в папке проекта
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "silero", "silero_hub")

_SILERO_HUB_DIR = _get_silero_hub_dir()
_SILERO_TORCH_HUB_DIR = os.path.expanduser("~/.cache/torch/hub/snakers4_silero-models_master")

def _load_silero_v5():
    """Загружаем Silero TTS из локального hub кэша.

    Приоритет по версии: v5_5_ru → v5_4_ru → v5_3_ru → v5_2_ru → v5_1_ru → v5_ru.
    Чем новее модель — тем лучше качество, поэтому по умолчанию берём самую свежую.
    """
    global SILERO_MODEL, SILERO_TTS_AVAILABLE, SILERO_MODEL_VERSION

    # Определяем путь к hub кэшу (приоритет: локальный в проекте, затем системный)
    hub_dir = None
    if os.path.exists(_SILERO_HUB_DIR):
        hub_dir = _SILERO_HUB_DIR
        logger.info(f"Silero: используем локальный кэш: {hub_dir}")
    elif os.path.exists(_SILERO_TORCH_HUB_DIR):
        hub_dir = _SILERO_TORCH_HUB_DIR
        logger.info(f"Silero: используем системный кэш: {hub_dir}")

    if not hub_dir:
        logger.info("Silero TTS: hub кэш не найден")
        return

    # Список моделей от самой новой к самой старой
    candidates = ["v5_5_ru", "v5_4_ru", "v5_3_ru", "v5_2_ru", "v5_1_ru", "v5_ru"]

    try:
        import torch
        for speaker_id in candidates:
            try:
                model, _ = torch.hub.load(
                    repo_or_dir=hub_dir,
                    model='silero_tts',
                    language='ru',
                    speaker=speaker_id,
                    trust_repo=True,
                    source='local'
                )
                SILERO_MODEL = model
                SILERO_TTS_AVAILABLE = True
                SILERO_MODEL_VERSION = speaker_id
                logger.info(f"Silero TTS {speaker_id} загружен. Спикеры: {model.speakers}")
                return
            except Exception as e_inner:
                logger.info(f"Silero {speaker_id} недоступна ({e_inner}), пробуем следующую...")
                continue
        logger.warning("Silero TTS: ни одна из моделей v5_5_ru..v5_ru не загрузилась")
    except Exception as e:
        logger.warning(f"Silero TTS ошибка загрузки: {e}")

# ─── Ленивая фоновая инициализация TTS (Silero + silero_pool + Qwen3) ──
# В старой версии эти тяжёлые инициализации (torch-загрузка модели Silero,
# spawn процессов silero_pool, сетевой доступ к Qwen3) выполнялись напрямую
# при импорте server_backup → блокировали `import server` и, как следствие,
# старт uvicorn на десятки секунд. Сделали их полностью ленивыми: поток
# поднимается только по первому обращению к синтезу речи либо через
# отложенный прогрев (не мешает подъёму HTTP-сервера).
_TTS_READY_EVENT = threading.Event()
_SILERO_POOL = None


def _publish_tts_state():
    """Скопировать изменившиеся TTS-глобалы в пакет `server` и в монолит.

    server/__init__.py копирует атрибуты server_backup ПО ЗНАЧЕНИЮ при
    импорте, поэтому ленивая инициализация (Silero/Qwen3) не видна через
    server.*: UI и speech_engine видели SILERO_TTS_AVAILABLE=False. После
    загрузки синхронизируем эти имена, если пакет `server` уже импортирован.

    Монолит — второй потребитель: с выносом этой секции в app/legacy/
    `PIPER_*`/`SILERO_*`/`TTS_ENGINE` перестали быть одним пространством имён,
    хвостовой `from legacy.rotating_logging import (...)` копирует их по значению
    один раз, а routes/admin.py, legacy_voice_ask и legacy_pages_tts_html читают
    именно `server.X`. Без зеркала они навечно видят False.
    """
    try:
        import sys as _sys
        _targets = [_sys.modules.get("server"), _sys.modules.get("server_backup")]
        _targets = [t for t in _targets if t is not None]
        if not _targets:
            return
        for _n, _v in (
            ("SILERO_TTS_AVAILABLE", SILERO_TTS_AVAILABLE),
            ("SILERO_MODEL", SILERO_MODEL),
            ("SILERO_MODEL_VERSION", SILERO_MODEL_VERSION),
            ("_SILERO_POOL", _SILERO_POOL),
            ("TTS_ENGINE", TTS_ENGINE),
            ("QWEN3_TTS_AVAILABLE", QWEN3_TTS_AVAILABLE),
            ("QWEN3_CLIENT", QWEN3_CLIENT),
            ("PIPER_TTS_AVAILABLE", PIPER_TTS_AVAILABLE),
            ("PIPER_VOICE", PIPER_VOICE),
            ("_PIPER_LOADED_PATH", _PIPER_LOADED_PATH),
        ):
            for _pkg in _targets:
                try:
                    setattr(_pkg, _n, _v)
                except Exception:
                    swallow.note('rotating_logging._publish_tts_state:L568')
                    pass
    except Exception:
        swallow.note('rotating_logging._publish_tts_state:L571')
        pass


def _init_tts_background():
    """Фоновая загрузка Silero (torch) + silero_pool + Qwen3."""
    global SILERO_MODEL, SILERO_TTS_AVAILABLE, SILERO_MODEL_VERSION
    global _SILERO_POOL, QWEN3_TTS_AVAILABLE, QWEN3_CLIENT, TTS_ENGINE
    try:
        _load_silero_v5()
    except Exception as _e:
        logger.warning(f"[tts-bg] Silero загрузка: {_e}")
    try:
        import silero_pool as _pool
        _pool.init_pool(_SILERO_HUB_DIR, workers=2)
        _SILERO_POOL = _pool
    except Exception as _e:
        _SILERO_POOL = None
        logger.warning(f"silero_pool недоступен: {_e}")
    try:
        _init_qwen3()
    except Exception as _e:
        logger.warning(f"[tts-bg] Qwen3 инициализация: {_e}")
    TTS_ENGINE = ("silero" if SILERO_TTS_AVAILABLE else
                  ("qwen3" if QWEN3_TTS_AVAILABLE else
                   ("piper" if PIPER_TTS_AVAILABLE else "webspeech")))
    # Сначала публикуем состояние (важно для server.* и UI), потом снимаем
    # флаг готовности — ожидающие потоки увидят уже актуальные значения.
    _publish_tts_state()
    _TTS_READY_EVENT.set()
    logger.info(f"TTS фоновая инициализация завершена (engine={TTS_ENGINE})")


def _ensure_tts_ready(timeout=60.0):
    """Дождаться готовности TTS, при необходимости ЛЕНИВО запустив инициализацию.

    Раньше инициализация стартовала сама при импорте модуля — это задерживало
    старт на ~12 с из-за загрузки весов Silero через torch. Теперь она
    поднимается здесь, при первом реальном обращении к синтезу речи.
    """
    _start_tts_init_async()
    if not _TTS_READY_EVENT.is_set():
        _TTS_READY_EVENT.wait(timeout)
    # На случай, если пакет `server` импортирован позже — синхронизируем ещё раз.
    _publish_tts_state()
    return True


# Синглтон-старт инициализации: флаг + Lock защищают от двойного запуска,
# если несколько запросов к TTS пришли одновременно.
_TTS_INIT_LOCK = threading.Lock()
_TTS_INIT_STARTED = False
_TTS_INIT_THREAD = None


def _start_tts_init_async():
    """Запустить фоновую инициализацию TTS, если она ещё не запускалась.

    Потокобезопасно: под защитой Lock проверяется флаг, поэтому параллельные
    запросы не создадут два потока загрузки. Возвращает True, если инициализация
    уже идёт или завершилась.
    """
    global _TTS_INIT_STARTED, _TTS_INIT_THREAD
    if _TTS_READY_EVENT.is_set():
        return True
    with _TTS_INIT_LOCK:
        if _TTS_INIT_STARTED:
            return True
        _TTS_INIT_STARTED = True
        _TTS_INIT_THREAD = threading.Thread(
            target=_init_tts_background, daemon=True, name="tts-bg-init")
        _TTS_INIT_THREAD.start()
        logger.info("TTS: ленивая инициализация запущена по первому обращению")
    return True


# ── Отложенный прогрев: старт сервера НЕ ждёт TTS, но и не оставляет UI
# без Silero навсегда. Через несколько секунд после старта инициализация
# идёт в фоне; /api/tts и speech_engine всё равно могут форсировать её сразу.
_TTS_WARMUP_DELAY = 12.0


def _warmup_tts_later(delay=None):
    """Через delay секунд поднять TTS в фоне (не блокирует старт)."""
    delay = _TTS_WARMUP_DELAY if delay is None else delay
    def _runner():
        try:
            if _TTS_READY_EVENT.wait(0):
                return
            time.sleep(delay)
            _start_tts_init_async()
        except Exception as _e:
            logger.debug(f"[tts-warmup] {_e}")
    threading.Thread(target=_runner, daemon=True, name="tts-warmup").start()

# Qwen3-TTS (через gradio_client)
QWEN3_TTS_AVAILABLE = False
QWEN3_CLIENT = None
QWEN3_URL = "http://127.0.0.1:7860"
QWEN3_DEFAULT_SPEAKER = "Вивиан — 21 год, студентка, приветливая, чуть мягкий голос, средний темп речи, плавные интонации"
QWEN3_DEFAULT_LANGUAGE = "Русский"
QWEN3_MODEL_SIZE = "1.7B"
QWEN3_SPEAKERS = [QWEN3_DEFAULT_SPEAKER]

def _qwen3_port_open(url, timeout=1.5):
    """Быстрая проверка, что Qwen3-TTS вообще слушает порт.

    Без неё gradio_client.Client() уходил в длинный таймаут соединения
    при каждом старте сервера — и ленивая инициализация TTS задерживалась
    на десятки секунд, хотя Qwen3 у пользователя обычно не запущен.
    """
    import socket
    from urllib.parse import urlparse
    u = urlparse(url if "//" in url else "http://" + url)
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        swallow.note('rotating_logging._qwen3_port_open:L691')
        return False


def _init_qwen3():
    global QWEN3_TTS_AVAILABLE, QWEN3_CLIENT, QWEN3_SPEERS
    try:
        if not _qwen3_port_open(QWEN3_URL):
            QWEN3_TTS_AVAILABLE = False
            QWEN3_CLIENT = None
            logger.info(f"Qwen3-TTS не запущен (порт {QWEN3_URL} не отвечает) — пропуск")
            return
        from gradio_client import Client
        # ИСПРАВЛЕНИЕ 30.09.2026: в gradio_client >= 1.0 параметр `timeout`
        # убран из Client.__init__ (в 2.6 его нет вовсе) — был TypeError и
        # Qwen3-TTS молча выключался. Пробуем варианты по убыванию новизны.
        QWEN3_CLIENT = None
        for kwargs in ({"httpx_kwargs": {"timeout": 15}}, {"timeout": 15}, {}):
            try:
                QWEN3_CLIENT = Client(QWEN3_URL, **kwargs)
                break
            except TypeError:
                swallow.note('rotating_logging._init_qwen3:L713')
                continue
        if QWEN3_CLIENT is None:
            raise RuntimeError("не удалось создать gradio_client.Client")
        QWEN3_TTS_AVAILABLE = True
        logger.info(f"Qwen3-TTS доступен на {QWEN3_URL}")
    except Exception as e:
        QWEN3_TTS_AVAILABLE = False
        logger.info(f"Qwen3-TTS не доступен: {e}")

def _qwen3_synthesize(text, speaker=None, language=None, temperature=0.7, top_p=0.9, max_tokens=2048):
    if not QWEN3_TTS_AVAILABLE or QWEN3_CLIENT is None:
        raise Exception("Qwen3-TTS не доступен")
    if not speaker:
        speaker = QWEN3_DEFAULT_SPEAKER
    if not language:
        language = QWEN3_DEFAULT_LANGUAGE
    result = QWEN3_CLIENT.predict(
        text, language, speaker, QWEN3_MODEL_SIZE,
        temperature, top_p, max_tokens,
        api_name="/generate_custom_voice"
    )
    import numpy as np
    import io, wave
    if isinstance(result, str):
        import soundfile as sf
        data, sr = sf.read(result)
        try:
            os.remove(result)
        except Exception:
            swallow.note('rotating_logging._qwen3_synthesize:L743')
            pass
    elif isinstance(result, (tuple, list)) and len(result) == 2:
        sr, data = result
    elif isinstance(result, bytes):
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(result))
    else:
        raise Exception(f"Qwen3-TTS неожиданный ответ: {type(result)}")
    if isinstance(data, np.ndarray):
        if data.ndim > 1:
            data = data.mean(axis=1)
        samples = data.astype(np.float32)
    else:
        samples = np.array(data, dtype=np.float32).flatten()
    audio_int16 = (samples * 32767).astype(np.int16)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sr))
        wav_file.writeframes(audio_int16.tobytes())
    wav_buffer.seek(0)
    return wav_buffer.read()

# init_qwen3 уже выполняется в фоне (см. _init_tts_background выше),
# чтобы не блокировать импорт server и старт uvicorn.

# Определяем доступный TTS движок (приоритет: silero > qwen3 > piper > webspeech).
# Если фоновая инициализация ещё не завершилась — значение будет пересчитано
# в _init_tts_background по завершении загрузки моделей.
TTS_ENGINE = "silero" if SILERO_TTS_AVAILABLE else ("qwen3" if QWEN3_TTS_AVAILABLE else ("piper" if PIPER_TTS_AVAILABLE else "webspeech"))
LM_STUDIO_TIMEOUT = _LM_CFG.get("timeout", 30)

# Пути из конфигурации
DATA_DIRS = CFG.get("data_dirs", [])
BOOKS_DIR = CFG.get("books_dir", os.path.expanduser("~/books"))
# Обход библиотек книг идёт по всем каталогам: скалярный books_dir вмещает
# только один, и свой путь профиля вытеснял общедоступный даже при включённой
# галочке «использовать общедоступные».
BOOKS_DIRS = list(CFG.get("books_dirs") or []) or [BOOKS_DIR]
os.makedirs(BOOKS_DIR, exist_ok=True)
# Диагностика: BOOKS_DIR перезаписывается в main.py при сохранении конфига
# (_g["BOOKS_DIR"] = _cfg.get("books_dir", ...)). Если библиотека книг пустеет,
# причина обычно здесь — смотрим это значение в логе при старте.
logger.info(f"BOOKS_DIR = {BOOKS_DIR} (exists={os.path.isdir(BOOKS_DIR)})")

BOOKS_DB_PATH = CFG.get("books_db_path", "")
if BOOKS_DB_PATH:
    try:
        from modules.collection_module import set_db_path
        set_db_path(BOOKS_DB_PATH)
    except Exception:
        swallow.note('rotating_logging.<module>:L796')
        pass

_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
CHAT_FOLDERS_FILE = os.path.join(NOTES_DIR, "_chat_folders.json")
# Каталог wiki-памяти. Раньше стояло os.path.join(_SERVER_DIR, "..", "wikimemory"),
# то есть <корень>\wikimemory — каталог не создавался, и
# /api/brain/graph-data отдавал 500 «Wiki directory not found», из-за чего
# страница /memory показывала экран ошибки. Теперь путь совпадает с тем, что
# использует main.py: INFINITY_WIKI_MEMORY_DIR → <каталог данных>/wikimemory.
# Переменные окружения здесь не доходят (маршрут обслуживает legacy-сервер
# в отдельном процессе), поэтому приоритет у config_module._root().
def _wikimemory_dir():
    env = os.environ.get("INFINITY_WIKI_MEMORY_DIR")
    if env:
        return os.path.abspath(env)
    try:
        from config_module import _root
        return os.path.join(_root(), "data", "wikimemory")
    except Exception:
        swallow.note('rotating_logging._wikimemory_dir:L816')
        return os.path.join(_SERVER_DIR, "..", "data", "wikimemory")


WIKI_MEMORY_DIR = _wikimemory_dir()
try:
    os.makedirs(WIKI_MEMORY_DIR, exist_ok=True)
except Exception:
    swallow.note('rotating_logging.<module>:L824')
    pass
HTML_DIRS = CFG.get("html_viewer_dirs", [])

# Настраиваем директорию для wget загрузок
try:
    from wget_module import set_downloads_dir
    if HTML_DIRS and len(HTML_DIRS) > 0:
        set_downloads_dir(HTML_DIRS[0])
        logger.info(f"Wget downloads dir: {HTML_DIRS[0]}")
except Exception:
    swallow.note('rotating_logging.<module>:L835')
    pass
APPS_DIRS = CFG.get("apps_dirs", [])
# Legacy ASIO отключён 30.09.2026 — заменён модулем Infinity Agent (/agentai).
# Переключатель: config.json → agent.legacy_asio.enabled = true возвращает
# старого агента. Файлы ASIO (agent_module/asio_core/asio_engine) на месте.
LEGACY_ASIO_ENABLED = bool(((CFG.get("agent") or {}).get("legacy_asio") or {}).get("enabled", False))
# Секция переименована hermes -> agentai; старая читается как запасной вариант,
# чтобы старый config.json не оставил страницу агента без редиректа.
HERMES_MOUNT = (((CFG.get("agentai") or CFG.get("hermes") or {}).get("mount")) or "/agentai").rstrip("/")
logger.info(f"[agent] legacy ASIO: {'ВКЛЮЧЁН' if LEGACY_ASIO_ENABLED else 'выключен'}"
            f" | модуль Hermes: {HERMES_MOUNT}")
# Resolve relative paths to absolute
_APBACK_DIR = os.path.dirname(os.path.abspath(__file__))
APPS_DIRS = [d if os.path.isabs(d) else os.path.join(_APBACK_DIR, d) for d in APPS_DIRS]
# Каталог приложений в корне проекта (runjson/app) — fallback, чтобы /api/apps
# и /api/apps/icon не оставались пустыми, если настроенные каталоги не существуют
_PROJECT_APPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
if _PROJECT_APPS_DIR not in APPS_DIRS:
    APPS_DIRS.append(_PROJECT_APPS_DIR)
# Профильные data-директории
APPS_DIR = CFG.get("apps_dir", "")
if APPS_DIR and APPS_DIR not in APPS_DIRS:
    APPS_DIRS.insert(0, APPS_DIR)
DEFAULT_APPS_DIR = CFG.get("default_apps_dir", "")
# Разрешаем относительный путь default_apps_dir относительно корня проекта
if DEFAULT_APPS_DIR and not os.path.isabs(DEFAULT_APPS_DIR):
    DEFAULT_APPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_APPS_DIR)
# Добавляем default_apps_dir в APPS_DIRS если ещё нет
if DEFAULT_APPS_DIR and DEFAULT_APPS_DIR not in APPS_DIRS:
    APPS_DIRS.append(DEFAULT_APPS_DIR)
HTML_DIR = CFG.get("html_dir", "")
EXTENSIONS_DIR = CFG.get("extensions_dir", "")
PHOTOS_DIR = CFG.get("photos_dir", "")
# Медиа директории
MEDIA_CFG = CFG.get("media", {})
MUSIC_DIRS = MEDIA_CFG.get("music_dirs", [])
VIDEO_DIRS = MEDIA_CFG.get("video_dirs", [])

# Автоматическое создание всех директорий
for _d in [NOTES_DIR, AI_DIR, CHAT_DIR, MEMORY_DIR, ATTACHMENTS_DIR, PLUGINS_DIR,
           os.path.join(NOTES_DIR, "_book_categories")]:
    os.makedirs(_d, exist_ok=True)
for _d in HTML_DIRS + APPS_DIRS:
    os.makedirs(_d, exist_ok=True)
for _d in [APPS_DIR, HTML_DIR, EXTENSIONS_DIR, PHOTOS_DIR]:
    if _d:
        os.makedirs(_d, exist_ok=True)

# ZIM directories
ZIM_DIRS = CFG.get("zim_dirs", [])

# Настройки индексации
_INDEX_CFG = CFG.get("index", {})
CUSTOM_INDEX_DIR = _INDEX_CFG.get("dir")
CUSTOM_FAISS_PATH = _INDEX_CFG.get("faiss_index_path")
CUSTOM_EMBEDDING_DB = _INDEX_CFG.get("embedding_db_path")
CUSTOM_DIMENSION = _INDEX_CFG.get("dimension", 768)

# Настройки поиска
_SEARCH_CFG = CFG.get("search", {})

# Настройки аутентификации
_AUTH_CFG = CFG.get("auth", {})
AUTH_ENABLED = _AUTH_CFG.get("enabled", False)
AUTH_USERNAME = _AUTH_CFG.get("username", "admin")
AUTH_PASSWORD_HASH = _AUTH_CFG.get("password_hash", "")
AUTH_TOKEN = _AUTH_CFG.get("token", "")


def _public_auth_cfg(auth_cfg):
    """Auth-настройки без секретов — hash и token не отдаём наружу."""
    auth_cfg = dict(auth_cfg or {})
    has_password = bool(auth_cfg.pop("password_hash", ""))
    has_token = bool(auth_cfg.pop("token", ""))
    auth_cfg["has_password"] = has_password
    auth_cfg["has_token"] = has_token
    return auth_cfg

# Гибридный поисковый движок v3 (Tantivy BM25 + Qdrant + RRF)
try:
    from search_engine_v2 import (
        HybridSearch, SearchIndex, FaissIndex, EmbeddingCache,
        collect_documents, stem_text, expand_query, format_search_results,
        extract_best_passages, detect_intent, get_intent_boost,
        WHOOSH_AVAILABLE, FAISS_AVAILABLE, INDEX_DIR, QDRANT_AVAILABLE,
        TANTIVY_AVAILABLE, CROSS_ENCODER_AVAILABLE, rerank_with_cross_encoder,
        _preprocess_fuzzy_query, _compute_recency_boost,
        correct_spelling, expand_query_with_synonyms, SYMSPELL_AVAILABLE,
        incremental_rebuild, detect_changes,
        PYMORPHY_AVAILABLE, lemmatize_word, NLTK_AVAILABLE,
        _LM_RERANKER_AVAILABLE, _LM_EMBED_AVAILABLE, _LM_RERANKER_MODEL_ID,
    )
    SEARCH_ENGINE_AVAILABLE = True
    SEARCH_ENGINE_VERSION = "v3_rrf_qdrant_tantivy"
except ImportError as e:
    logger.warning(f"Поисковый движок v3 недоступен: {e}")
    SEARCH_ENGINE_AVAILABLE = False
    SEARCH_ENGINE_VERSION = "none"
    HybridSearch = None
    extract_best_passages = None
    detect_intent = None
    get_intent_boost = None
    INDEX_DIR = None
    SEARCH_ENGINE_VERSION = "none"
    FAISS_AVAILABLE = False
    WHOOSH_AVAILABLE = False
    TANTIVY_AVAILABLE = False
    QDRANT_AVAILABLE = False
    CROSS_ENCODER_AVAILABLE = False
    SYMSPELL_AVAILABLE = False
    PYMORPHY_AVAILABLE = False
    NLTK_AVAILABLE = False
    _LM_RERANKER_AVAILABLE = False
    _LM_EMBED_AVAILABLE = False
    _LM_RERANKER_MODEL_ID = None

# HTML модуль
HTML_AVAILABLE = False
try:
    from html_module import (
        find_html_files, search_html_files, extract_html_meta, extract_html_text,
        get_html_files_cached
    )
    HTML_AVAILABLE = True
    # Viewer с деревом папок и wget. Источник правды один —
    # templates/page_html_viewer.html (html_viewer_page его читает).
    from html_viewer_page import PAGE_HTML_VIEWER_NEW as PAGE_HTML_VIEWER
    from html_apps_pages import PAGE_APPS
    from html_apps_pages import PAGE_HTML_LANDING
except ImportError as e:
    logger.warning(f"HTML модуль недоступен: {e}")
    from html_apps_pages import PAGE_HTML_LANDING, PAGE_APPS
    # Страница просмотрщика — тот же шаблон, а не устаревшая копия
    # (в копии из html_apps_pages файлового менеджера не было вовсе).
    try:
        from html_viewer_page import PAGE_HTML_VIEWER_NEW as PAGE_HTML_VIEWER
    except Exception:
        swallow.note('rotating_logging.<module>:L973')
        PAGE_HTML_VIEWER = ""

# Медиа модуль
MEDIA_AVAILABLE = False
try:
    import media_module as MM
    scan_music_directories = MM.scan_music_directories
    scan_video_directories = MM.scan_video_directories
    rebuild_music_index = MM.rebuild_music_index
    rebuild_video_index = MM.rebuild_video_index
    clear_music_index = MM.clear_music_index
    clear_video_index = MM.clear_video_index
    sync_media_dirs_from_config = MM.sync_media_dirs_from_config
    get_music_index = MM.get_music_index
    get_video_index = MM.get_video_index
    group_tracks_by_artist = MM.group_tracks_by_artist
    group_tracks_by_album = MM.group_tracks_by_album
    group_tracks_by_genre = MM.group_tracks_by_genre
    search_tracks = MM.search_tracks
    get_lyrics = MM.get_lyrics
    save_lyrics = MM.save_lyrics
    get_track_filepath = MM.get_track_filepath
    fetch_lyrics_from_internet = MM.fetch_lyrics_from_internet
    get_artist_info = MM.get_artist_info
    save_artist_info = MM.save_artist_info
    get_artist_tracks = MM.get_artist_tracks
    get_artist_albums = MM.get_artist_albums
    get_favorites = MM.get_favorites
    add_to_favorites = MM.add_to_favorites
    remove_from_favorites = MM.remove_from_favorites
    is_favorite = MM.is_favorite
    get_playlists = MM.get_playlists
    create_playlist = MM.create_playlist
    get_playlist = MM.get_playlist
    update_playlist = MM.update_playlist
    add_track_to_playlist = MM.add_track_to_playlist
    remove_track_from_playlist = MM.remove_track_from_playlist
    delete_playlist = MM.delete_playlist
    INDEX_PROGRESS = MM.INDEX_PROGRESS
    find_duplicates = MM.find_duplicates
    get_recommendations = MM.get_recommendations
    _save_track_cover = MM._save_track_cover
    COVERS_DIR = os.path.join(MM.MEDIA_DIR, "covers")
    fetch_movie_metadata = MM.fetch_movie_metadata
    get_movie_metadata_cached = MM.get_movie_metadata_cached
    POSTERS_DIR = MM.POSTERS_DIR
    BACKDROPS_DIR = MM.BACKDROPS_DIR
    MOVIE_CACHE_FILE = MM.MOVIE_CACHE_FILE
    MEDIA_DIR = MM.MEDIA_DIR
    MUSIC_INDEX_FILE = MM.MUSIC_INDEX_FILE
    VIDEO_INDEX_FILE = MM.VIDEO_INDEX_FILE
    LYRICS_DIR = MM.LYRICS_DIR
    ARTISTS_DIR = MM.ARTISTS_DIR
    PLAYLISTS_DIR = MM.PLAYLISTS_DIR
    FAVORITES_FILE = MM.FAVORITES_FILE
    _save_json = MM._save_json
    # Новые функции
    check_video_file_health = MM.check_video_file_health
    get_media_statistics = MM.get_media_statistics
    find_random_video = MM.find_random_video
    convert_avi_videos = MM.convert_avi_videos
    start_transcode_server = MM.start_transcode_server
    save_series_server = MM.save_series_server
    delete_track_server = MM.delete_track_server
    log_play_server = MM.log_play_server
    log_watch_server = MM.log_watch_server
    add_to_collection_direct = MM.add_to_collection_direct
    remove_from_collection_direct = MM.remove_from_collection_direct
    delete_collection_direct = MM.delete_collection_direct
    PAGE_MEDIA = MM.PAGE_MEDIA
    MEDIA_AVAILABLE = True
    logger.info("Медиа модуль загружен")
except ImportError as e:
    logger.warning(f"Медиа модуль недоступен: {e}")

# Photo модуль
PHOTO_AVAILABLE = False
PHOTO_DIRS = []
try:
    import photo_module as PHOTO
    _photo_cfg_dirs = CFG.get("media", {}).get("photo_dirs", [])
    if _photo_cfg_dirs:
        PHOTO_DIRS = _photo_cfg_dirs
        PHOTO.set_photo_dirs_from_config(_photo_cfg_dirs)
    else:
        PHOTO_DIRS = PHOTO.get_photo_dirs()
    PHOTO_AVAILABLE = True
    logger.info("Photo модуль загружен")
except ImportError as e:
    logger.warning(f"Photo модуль недоступен: {e}")
except Exception as e:
    logger.warning(f"Photo модуль ошибка: {e}")

# News модуль
NEWS_AVAILABLE = False
try:
    import news_module
    from news_module import (
        get_rss_sources, add_rss_source, remove_rss_source, toggle_source,
        search_rss_feeds, discover_rss_from_url,
        fetch_feed_articles, fetch_all_feeds, start_auto_refresh,
        get_articles, get_article, get_categories,
        start_digest_scheduler,
        PAGE_NEWS
    )
    NEWS_AVAILABLE = True
    logger.info("News модуль загружен")
except ImportError as e:
    logger.warning(f"News модуль недоступен: {e}")

# ── Ленивый запуск планировщиков новостей ───────────────────────────────────
# Раньше start_auto_refresh() и start_digest_scheduler() вызывались прямо в
# теле модуля (строки ниже). Они поднимают сетевые обходы RSS, и на старте это
# давало пачки SSL-таймаутов по 10-30 с, задерживая готовность сервера.
# Теперь планировщики стартуют лениво: при первом обращении к разделу новостей
# либо из фонового потока с задержкой после подъёма uvicorn (чтобы дайджесты
# и автообновление продолжали работать как раньше).
_NEWS_SCHED_LOCK = threading.Lock()
_NEWS_SCHED_STARTED = False

def _ensure_news_schedulers():
    """Однократно запустить планировщики новостей (потокобезопасно)."""
    global _NEWS_SCHED_STARTED
    if _NEWS_SCHED_STARTED:
        return True
    with _NEWS_SCHED_LOCK:
        if _NEWS_SCHED_STARTED:
            return True
        _NEWS_SCHED_STARTED = True
    try:
        # Автообновление по настройке (auto_refresh_minutes)
        start_auto_refresh()
        # Планировщик утреннего дайджеста (по расписанию пользователей)
        start_digest_scheduler()
    except Exception as _e:
        logger.warning(f"[news] не удалось запустить планировщики: {_e}")
    return True

def _start_news_schedulers_later(delay=15.0):
    """Фоновый поток: поднять планировщики новостей через delay секунд."""
    def _runner():
        try:
            time.sleep(delay)
            _ensure_news_schedulers()
        except Exception as _e:
            logger.debug(f"[news-sched] {_e}")
    threading.Thread(target=_runner, daemon=True, name="news-sched").start()

# Apps модуль
APPS_AVAILABLE = False
try:
    from apps_module import (
        find_apps, start_app_server, stop_app_server, get_running_servers,
        get_apps_cached, load_app_json, share_app
    )
    APPS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Apps модуль недоступен: {e}")

# LM Studio config — из config.json (не хардкод!)
# Переопределены выше из _LM_CFG на строках 66-72



# libzim для ZIM файлов
ZIM_AVAILABLE = False
try:
    from libzim.reader import Archive, Entry
    from libzim.search import Query, Searcher
    ZIM_AVAILABLE = True
except ImportError:
    swallow.note('rotating_logging.<module>:L1145')
    pass

# Библиотеки для книг
BOOKS_AVAILABLE = {}
try:
    import ebooklib
    from ebooklib import epub
    BOOKS_AVAILABLE['epub'] = True
except ImportError:
    swallow.note('rotating_logging.<module>:L1155')
    pass
try:
    from bs4 import BeautifulSoup
    BOOKS_AVAILABLE['bs4'] = True
except ImportError:
    swallow.note('rotating_logging.<module>:L1161')
    pass
try:
    from lxml import etree
    BOOKS_AVAILABLE['lxml'] = True
except ImportError:
    swallow.note('rotating_logging.<module>:L1167')
    pass
try:
    import PyPDF2
    BOOKS_AVAILABLE['pdf'] = True
except ImportError:
    swallow.note('rotating_logging.<module>:L1173')
    pass
try:
    import docx
    BOOKS_AVAILABLE['docx'] = True
except ImportError:
    swallow.note('rotating_logging.<module>:L1179')
    pass
# DjVu через djvutxt subprocess (python3-djvu 0.9.0 несовместим)
_DJVU_BACKEND = None
import shutil
if shutil.which('djvutxt'):
    BOOKS_AVAILABLE['djvu'] = True
    _DJVU_BACKEND = 'djvutxt'


# Имена из других секций монолита (только чтение). Их связывает хвост
# server_backup вызовом _bind(): к этому моменту связаны уже все
# ре-экспорты, поэтому порядок секций не важен. При прямом импорте этого
# модуля _bind() подтянет их из догруженного server_backup сам.
_FOREIGN = (
    'RotatingFileHandler',
    'get_api_key',
    'json',
    'logging',
    'normalize_openai_base',
    'os',
    'sys',
    'threading',
    'time',
)

def _bind(namespace=None):
    if namespace is None:
        import server_backup as _sb
        namespace = vars(_sb)
    _g = globals()
    for _n in _FOREIGN:
        if _n not in _g and _n in namespace:
            _g[_n] = namespace[_n]
    return [n for n in _FOREIGN if n not in globals()]
