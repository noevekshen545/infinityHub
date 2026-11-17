import sys, os, json, threading, io, wave, re

import server_backup as _original
import swallow

logger = _original.logger
CFG = _original.CFG

# --- Piper TTS ---
PIPER_TTS_AVAILABLE = False
PIPER_VOICE = None
_PIPER_VOICE_CACHE = {}
_PIPER_VOICE_CACHE_LOCK = threading.Lock()

def _get_piper_model_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        _portable = os.path.join(os.environ.get("INFINITY_ROOT", ""), "models", "piper")
        if os.path.isdir(_portable):
            return _portable
        return os.path.join(sys._MEIPASS, "models", "piper")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "piper")

def _get_piper_cache_dir():
    return os.path.expanduser("~/.infinity/piper_models")

def _find_piper_model(voice_id=None):
    project_dir = _get_piper_model_dir()
    cache_dir = _get_piper_cache_dir()
    dirs = [(project_dir, "project"), (cache_dir, "cache")]
    if voice_id:
        for d, source in dirs:
            mp_ = os.path.join(d, voice_id + ".onnx")
            cp_ = os.path.join(d, voice_id + ".onnx.json")
            if os.path.exists(mp_):
                return mp_, cp_ if os.path.exists(cp_) else None, source
        return None, None, None
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
    voices = []
    seen = set()
    for d, source in [(_get_piper_model_dir(), "project"), (_get_piper_cache_dir(), "cache")]:
        if not os.path.isdir(d):
            continue
        try:
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".onnx") or fn.endswith(".onnx.json"):
                    continue
                voice_id = fn[:-5]
                if voice_id in seen:
                    continue
                seen.add(voice_id)
                path = os.path.join(d, fn)
                try:
                    size_mb = round(os.path.getsize(path) / (1024 * 1024), 1)
                except Exception:
                    swallow.note('tts._list_piper_voices:L61')
                    size_mb = 0
                desc = ""
                json_path = os.path.join(d, fn + ".json")
                if os.path.exists(json_path):
                    try:
                        with open(json_path, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
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
                        desc = " \u00b7 ".join(desc_parts)
                    except Exception:
                        swallow.note('tts._list_piper_voices:L83')
                        pass
                voices.append({"id": voice_id, "name": voice_id, "source": source, "size_mb": size_mb, "description": desc})
        except Exception as e:
            logger.warning(f"Piper: не удалось прочитать {d}: {e}")
    return voices

def _get_piper_voice(voice_id=None):
    global PIPER_VOICE
    model_path, config_path, source = _find_piper_model(voice_id)
    if not model_path:
        return None
    cache_key = model_path
    with _PIPER_VOICE_CACHE_LOCK:
        if cache_key in _PIPER_VOICE_CACHE:
            return _PIPER_VOICE_CACHE[cache_key]
        try:
            from piper import PiperVoice as _PV
            voice = _PV.load(model_path, config_path=config_path)
            _PIPER_VOICE_CACHE[cache_key] = voice
            PIPER_VOICE = voice
            return voice
        except Exception as e:
            logger.error(f"Piper: не удалось загрузить {model_path}: {e}")
            return None

try:
    from piper import PiperVoice
    _PIPER_MODEL, _PIPER_CONFIG, _PIPER_SOURCE = _find_piper_model()
    if _PIPER_MODEL and os.path.exists(_PIPER_MODEL):
        PIPER_VOICE = _get_piper_voice()
        if PIPER_VOICE is not None:
            PIPER_TTS_AVAILABLE = True
            logger.info(f"Piper TTS загружен: {_PIPER_MODEL} (источник: {_PIPER_SOURCE})")
    else:
        logger.info("Piper TTS: модель не найдена. Положите в models/piper/ или ~/.infinity/piper_models/")
except ImportError:
    swallow.note('tts.<module>:L120')
    pass
except Exception as e:
    logger.warning(f"Piper TTS ошибка: {e}")

# --- Silero TTS v5 ---
# ВНИМАНИЕ: загрузка модели НЕ выполняется на импорте.
# torch (~3.7 с) + transformers (~6.5 с) тянутся вместе с silero_pool,
# и на старте сервера это была самая дорогая операция — ~10 из 16 секунд
# импорта. Теперь модель грузится лениво, при ПЕРВОМ синтезе речи,
# через silero_tts_ready(); фоновую инициализацию делает
# server_backup._init_tts_background().
SILERO_TTS_AVAILABLE = False
SILERO_MODEL = None
SILERO_MODEL_VERSION = "v5_6_ru"
SILERO_SAMPLE_RATE = 48000
_SILERO_POOL = None
_SILERO_TRIED = False
_SILERO_LOCK = threading.Lock()

def _get_silero_hub_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        _portable = os.path.join(os.environ.get("INFINITY_ROOT", ""),
                                 "models", "silero", "silero_hub")
        if os.path.isdir(_portable):
            return _portable
        return os.path.join(sys._MEIPASS, "models", "silero", "silero_hub")
    # Переносимая сборка: кэш Silero лежит в <корень>/models/silero/silero_hub,
    # иначе синтез молча не находит модель и уходит в браузерный webspeech.
    try:
        from config_module import _root
        _portable = os.path.join(_root(), "models", "silero", "silero_hub")
        if os.path.isdir(_portable):
            return _portable
    except Exception:
        swallow.note('tts._get_silero_hub_dir:L151')
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "silero", "silero_hub")

_SILERO_HUB_DIR = _get_silero_hub_dir()
_SILERO_TORCH_HUB_DIR = os.path.expanduser("~/.cache/torch/hub/snakers4_silero-models_master")

def _load_silero_v5():
    """Загрузить Silero и silero_pool. Идемпотентна и потокобезопасна."""
    global SILERO_MODEL, SILERO_TTS_AVAILABLE, SILERO_MODEL_VERSION
    global _SILERO_POOL, _SILERO_TRIED
    with _SILERO_LOCK:
        if _SILERO_TRIED:
            return SILERO_TTS_AVAILABLE
        _SILERO_TRIED = True
        try:
            hub_dir = None
            if os.path.exists(_SILERO_HUB_DIR):
                hub_dir = _SILERO_HUB_DIR
                logger.info(f"Silero: используем локальный кэш: {hub_dir}")
            elif os.path.exists(_SILERO_TORCH_HUB_DIR):
                hub_dir = _SILERO_TORCH_HUB_DIR
                logger.info(f"Silero: используем системный кэш: {hub_dir}")
            if not hub_dir:
                logger.info("Silero TTS: hub кэш не найден")
            else:
                candidates = ["v5_6_ru", "v5_5_ru", "v5_4_ru",
                              "v5_3_ru", "v5_2_ru", "v5_1_ru", "v5_ru"]
                try:
                    import torch
                    for speaker_id in candidates:
                        try:
                            model, _ = torch.hub.load(
                                repo_or_dir=hub_dir, model='silero_tts',
                                language='ru', speaker=speaker_id,
                                trust_repo=True, source='local'
                            )
                            SILERO_MODEL = model
                            SILERO_TTS_AVAILABLE = True
                            SILERO_MODEL_VERSION = speaker_id
                            logger.info(f"Silero TTS {speaker_id} загружен. "
                                        f"Спикеры: {model.speakers}")
                            break
                        except Exception as e_load:
                            # Причина отказа обязательно логируется: иначе видно
                            # только итог («загружена старая модель»), а почему
                            # новая не подошла — непонятно.
                            logger.warning(
                                f"Silero TTS {speaker_id}: не загрузилась "
                                f"({type(e_load).__name__}: {str(e_load)[:200]})"
                            )
                            continue
                    else:
                        logger.warning("Silero TTS: ни одна из моделей "
                                       "v5_6_ru..v5_ru не загрузилась")
                except Exception as e:
                    logger.warning(f"Silero TTS ошибка загрузки: {e}")
        finally:
            try:
                import silero_pool
                silero_pool.init_pool(_SILERO_HUB_DIR, workers=2)
                _SILERO_POOL = silero_pool
            except Exception as _e:
                logger.warning(f"silero_pool недоступен: {_e}")
                _SILERO_POOL = None
    return SILERO_TTS_AVAILABLE


def silero_tts_ready():
    """Дождаться готовности Silero, при необходимости загрузив модель.

    Без этого синтез речи сразу после старта успевал ответить «Silero
    недоступен»: фоновой инициализации ещё не было, а раньше модель
    грузилась на импорте. Теперь вызов догружает её синхронно.
    """
    if SILERO_TTS_AVAILABLE or _SILERO_POOL is not None:
        return True
    _load_silero_v5()
    return bool(SILERO_TTS_AVAILABLE or _SILERO_POOL is not None)

def _list_silero_speakers():
    if SILERO_MODEL is None:
        return []
    return list(SILERO_MODEL.speakers or [])

_TTS_JUNK_RE = re.compile(r"(\*{1,3}|_{1,3}|#{1,6}|={1,3}|`{1,3}|~{2,}|\.{3,}|-{2,}|—{2,}|<[^>]+>)")

def _sanitize_tts_text(text: str) -> str:
    """Очистка текста для TTS от Markdown и символов вне токенсета Silero RU
    (цифры, '-', '()', кавычки) — иначе apply_tts падает с KeyError."""
    if not text:
        return ""
    from silero_pool import silero_safe_text
    return silero_safe_text(text)

def _apply_silero_rate_pitch(samples, sample_rate, rate=1.0, pitch=0.0):
    if samples is None or len(samples) == 0:
        return samples
    rate = float(rate) if rate else 1.0
    pitch = float(pitch) if pitch else 0.0
    if rate < 0.25: rate = 0.25
    if rate > 4.0: rate = 4.0
    if pitch < -24: pitch = -24
    if pitch > 24: pitch = 24
    if abs(rate - 1.0) < 0.01 and abs(pitch) < 0.01:
        return samples
    import numpy as np
    out = np.asarray(samples, dtype=np.float32)
    if abs(pitch) >= 0.01:
        try:
            import librosa
            out = librosa.effects.pitch_shift(out, sr=sample_rate, n_steps=pitch)
        except ImportError:
            swallow.note('tts._apply_silero_rate_pitch:L264')
            try:
                factor = 2.0 ** (pitch / 12.0)
                new_len = max(1, int(len(out) / factor))
                idx = np.linspace(0, len(out) - 1, new_len).astype(np.int64)
                shifted = out[idx]
                idx2 = np.linspace(0, len(shifted) - 1, len(out)).astype(np.int64)
                out = shifted[idx2]
            except Exception:
                swallow.note('tts._apply_silero_rate_pitch:L273')
                pass
        except Exception:
            swallow.note('tts._apply_silero_rate_pitch:L276')
            pass
    if abs(rate - 1.0) >= 0.01:
        try:
            target_len = max(1, int(len(out) / rate))
            idx = np.linspace(0, len(out) - 1, target_len).astype(np.int64)
            out = out[idx]
        except Exception:
            swallow.note('tts._apply_silero_rate_pitch:L284')
            pass
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 1.0:
        out = out / peak
    return out.astype(np.float32)

def _apply_silero_rate_pitch_to_wav(wav_bytes, rate=1.0, pitch=0.0):
    if abs(float(rate or 1.0) - 1.0) < 0.01 and abs(float(pitch or 0.0)) < 0.01:
        return wav_bytes
    try:
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

# --- Qwen3-TTS ---
QWEN3_TTS_AVAILABLE = False
QWEN3_CLIENT = None
QWEN3_URL = "http://127.0.0.1:7860"
QWEN3_DEFAULT_SPEAKER = "Вивиан — 21 год, студентка, приветливая, чуть мягкий голос, средний темп речи, плавные интонации"
QWEN3_DEFAULT_LANGUAGE = "Русский"
QWEN3_MODEL_SIZE = "1.7B"
QWEN3_SPEAKERS = [QWEN3_DEFAULT_SPEAKER]

def _init_qwen3():
    global QWEN3_TTS_AVAILABLE, QWEN3_CLIENT, QWEN3_SPEAKERS
    try:
        from gradio_client import Client
        QWEN3_CLIENT = Client(QWEN3_URL, timeout=15)
        QWEN3_TTS_AVAILABLE = True
        logger.info(f"Qwen3-TTS доступен на {QWEN3_URL}")
    except Exception as e:
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
    if isinstance(result, str):
        import soundfile as sf
        data, sr = sf.read(result)
        try:
            os.remove(result)
        except Exception:
            swallow.note('tts._qwen3_synthesize:L355')
            pass
    elif isinstance(result, (tuple, list)) and len(result) == 2:
        sr, data = result
    elif isinstance(result, bytes):
        import soundfile as sf
        data, sr = sf.read(io.BytesIO(result))
    else:
        raise Exception(f"Qwen3-TTS неожиданный ответ: {type(result)}")
    import numpy as np
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

_init_qwen3()

TTS_ENGINE = "silero" if SILERO_TTS_AVAILABLE else ("qwen3" if QWEN3_TTS_AVAILABLE else ("piper" if PIPER_TTS_AVAILABLE else "webspeech"))
