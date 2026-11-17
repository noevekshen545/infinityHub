"""
Speech Engine — централизованный реестр и диспетчер движков
распознавания (STT) и синтеза (TTS) речи.

Используется main.py (FastAPI) и всеми модулями приложения.
Состояние локальных движков берётся из пакета `server`
(server_backup.py): Silero, Piper, Qwen3-TTS, faster-whisper (stt_engine).

Конфигурация: config_module.get_speech() — секция "speech" конфига.
Поддерживаются кастомные API-движки:
  * STT:  OpenAI-совместимый POST {url}/audio/transcriptions (multipart)
  * TTS:  OpenAI-совместимый POST {url} (JSON {text,voice,...}) или GET,
          ответ — аудиофайл (wav/mp3) либо JSON {audio: base64}
"""
import os
import io
import json
import base64
import logging
import tempfile
from typing import Optional
import swallow

logger = logging.getLogger("infinity")


def _server():
    """Ленивый импорт пакета server (избегаем циклических импортов)."""
    import server
    return server


def get_speech_config():
    """Конфиг речи с фоллбэком на legacy ui.speech."""
    from config_module import get_speech
    return get_speech()


# ---------------------------------------------------------------- TTS

def resolve_tts_engine(preferred: str = None) -> str:
    """Определить фактический TTS-движок.

    Приоритет: preferred (параметр запроса) -> конфиг tts_engine -> авто.
    'auto' резолвится: silero > piper > qwen3 > webspeech.
    Кастомные движки обозначаются как 'custom:<id>'.
    """
    from config_module import get_config
    cfg = get_speech_config()
    engine = preferred or cfg.get("tts_engine") or "auto"
    if engine == "auto":
        S = _server()
        if S.SILERO_TTS_AVAILABLE:
            return "silero"
        if S.PIPER_TTS_AVAILABLE:
            return "piper"
        if S.QWEN3_TTS_AVAILABLE:
            return "qwen3"
        return "webspeech"
    return engine


def list_custom_tts():
    cfg = get_speech_config()
    out = []
    for eng in cfg.get("custom_tts_engines") or []:
        if not isinstance(eng, dict):
            continue
        out.append({
            "id": eng.get("id", ""),
            "name": eng.get("name", eng.get("id", "Кастомный TTS")),
            "url": eng.get("url", ""),
            "voice": eng.get("voice", ""),
            "model": eng.get("model", ""),
            "format": eng.get("format", "wav"),
            "has_key": bool(eng.get("api_key")),
        })
    return out


def _synth_silero(text: str, speaker: str = None, rate: float = 1.0, pitch: float = 0.0) -> bytes:
    """Синтез через Silero (in-process модель или silero_pool).

    silero_pool.synth() возвращает готовый WAV (bytes), in-process
    apply_tts возвращает float32-сэмплы — оба пути приводятся к WAV.
    """
    S = _server()
    # Ленивая инициализация: при первом реальном синтезе поднимаем Silero,
    # если он ещё не загружен (на старте сервера TTS не инициализируется).
    try:
        S._ensure_tts_ready(timeout=120)
    except Exception:
        swallow.note('speech_engine._synth_silero:L92')
        pass
    if not S.SILERO_TTS_AVAILABLE:
        raise RuntimeError("Silero TTS недоступен")
    speaker = speaker or get_speech_config().get("tts", {}).get("silero_speaker") or "xenia"
    import numpy as np
    import torch
    import wave
    _pool = getattr(S, "_SILERO_POOL", None)
    if _pool is not None and _pool.is_ready():
        wav_bytes = _pool.synth(text, speaker, 48000)
        if (rate and float(rate) != 1.0) or (pitch and float(pitch) != 0.0):
            try:
                wav_bytes = S._apply_silero_rate_pitch_to_wav(wav_bytes, float(rate or 1.0), float(pitch or 0.0))
            except Exception:
                swallow.note('speech_engine._synth_silero:L107')
                pass
        return wav_bytes
    if speaker not in (S.SILERO_MODEL.speakers or []):
        speaker = "xenia"
    from silero_pool import silero_safe_text, silero_allowed_symbols
    safe_text = silero_safe_text(text, silero_allowed_symbols(S.SILERO_MODEL))
    if len(safe_text) < 2:
        raise RuntimeError("Silero: текст пуст после очистки от неподдерживаемых символов")
    audio = S.SILERO_MODEL.apply_tts(text=safe_text, speaker=speaker, sample_rate=48000)
    if isinstance(audio, torch.Tensor):
        audio = audio.numpy().flatten()
    else:
        audio = np.array(audio).flatten()
    sample_rate = 48000
    audio = np.asarray(audio, dtype=np.float32).flatten()
    if audio.size == 0:
        raise RuntimeError("Silero вернул пустой результат")
    if (rate and float(rate) != 1.0) or (pitch and float(pitch) != 0.0):
        try:
            audio = S._apply_silero_rate_pitch(audio, sample_rate, rate or 1.0, pitch or 0.0)
        except Exception:
            swallow.note('speech_engine._synth_silero:L129')
            pass
    audio_int16 = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def _synth_piper(text: str, voice_id: str = None, rate: float = 1.0, pitch: float = 0.0) -> bytes:
    """Синтез через Piper (локальные голоса).

    Обрабатывает все форматы чанков piper-python: объект с audio_float_array /
    audio_int16_array / audio_int16_bytes / audio / numpy, отдельные ndarray
    и сырые bytes (int16).
    """
    S = _server()
    if not S.PIPER_TTS_AVAILABLE:
        raise RuntimeError("Piper TTS недоступен")
    voice_id = voice_id or get_speech_config().get("tts", {}).get("piper_voice") or ""
    voice = None
    if voice_id and hasattr(S, "_get_piper_voice"):
        try:
            voice = S._get_piper_voice(voice_id)
        except Exception:
            swallow.note('speech_engine._synth_piper:L157')
            voice = None
    if voice is None:
        voice = S.PIPER_VOICE
    if voice is None:
        raise RuntimeError("Piper голос не загружен")
    import numpy as np
    import wave
    sample_rate = int(getattr(voice, "config", None).sample_rate if getattr(voice, "config", None) else 22050)
    all_audio = []
    try:
        chunks = voice.synthesize(text)
        if not hasattr(chunks, "__iter__") or isinstance(chunks, np.ndarray):
            chunks = [chunks]
        for audio_chunk in chunks:
            if audio_chunk is None:
                continue
            if hasattr(audio_chunk, "audio_float_array"):
                samples = audio_chunk.audio_float_array
                samples = np.asarray(samples, dtype=np.float32).flatten()
                all_audio.append(samples)
            elif hasattr(audio_chunk, "audio_int16_array"):
                samples = np.asarray(audio_chunk.audio_int16_array, dtype=np.int16).flatten()
                all_audio.append(samples.astype(np.float32) / 32767.0)
            elif hasattr(audio_chunk, "audio_int16_bytes"):
                samples = np.frombuffer(bytes(audio_chunk.audio_int16_bytes), dtype=np.int16)
                all_audio.append(samples.astype(np.float32) / 32767.0)
            elif hasattr(audio_chunk, "audio"):
                arr = audio_chunk.audio
                if hasattr(arr, "numpy"):
                    arr = arr.numpy()
                samples = np.asarray(arr).flatten()
                if samples.dtype != np.float32:
                    samples = samples.astype(np.float32)
                all_audio.append(samples)
            elif hasattr(audio_chunk, "numpy"):
                samples = np.asarray(audio_chunk.numpy()).flatten()
                if samples.dtype != np.float32:
                    samples = samples.astype(np.float32)
                all_audio.append(samples)
            elif isinstance(audio_chunk, (bytes, bytearray)):
                samples = np.frombuffer(bytes(audio_chunk), dtype=np.int16)
                all_audio.append(samples.astype(np.float32) / 32767.0)
            elif isinstance(audio_chunk, np.ndarray):
                samples = np.asarray(audio_chunk).flatten()
                if samples.dtype != np.float32:
                    samples = samples.astype(np.float32)
                all_audio.append(samples)
    except TypeError:
        # старые версии piper-python: voice.synthesize(text) возвращает np.ndarray
        swallow.note('speech_engine._synth_piper:L206')
        audio = np.asarray(voice.synthesize(text)).flatten()
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        all_audio = [audio]
    all_audio = [a for a in all_audio if a is not None and a.size > 0]
    if not all_audio:
        raise RuntimeError("Piper вернул пустой результат")
    audio_float = np.concatenate(all_audio)
    if (rate and float(rate) != 1.0) or (pitch and float(pitch) != 0.0):
        try:
            audio_float = S._apply_silero_rate_pitch(audio_float, sample_rate, rate or 1.0, pitch or 0.0)
        except Exception:
            swallow.note('speech_engine._synth_piper:L220')
            pass
    audio_int16 = (audio_float * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def _synth_qwen3(text: str, speaker: str = None, url: str = None) -> bytes:
    """Синтез через Qwen3-TTS (gradio_client)."""
    S = _server()
    if not S.QWEN3_TTS_AVAILABLE:
        raise RuntimeError("Qwen3-TTS недоступен")
    if url:
        try:
            S.QWEN3_URL = url
            S._init_qwen3()
        except Exception as e:
            logger.warning(f"Qwen3-TTS: не удалось переинициализировать клиент ({e})")
    if not S.QWEN3_TTS_AVAILABLE or S.QWEN3_CLIENT is None:
        raise RuntimeError("Qwen3-TTS не доступен")
    speaker = speaker or get_speech_config().get("tts", {}).get("qwen3_speaker") or ""
    return S._qwen3_synthesize(text, speaker=speaker or None)


def _synth_custom(text: str, engine: dict) -> bytes:
    """Синтез через кастомный HTTP TTS-движок."""
    import httpx
    url = (engine.get("url") or "").strip()
    if not url:
        raise RuntimeError("Кастомный TTS: не указан URL")
    api_key = engine.get("api_key") or ""
    voice = engine.get("voice") or ""
    model = engine.get("model") or ""
    fmt = engine.get("format") or "wav"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {"text": text, "input": text}
    if voice:
        payload["voice"] = voice
    if model:
        payload["model"] = model
    is_openai_like = "/v1/audio/speech" in url
    if is_openai_like:
        payload["model"] = model or "tts-1"
        payload["format"] = "wav" if fmt in ("wav", "wave") else "mp3"
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=180)
        resp.raise_for_status()
    except Exception:
        # GET-фоллбэк (URL с подстановкой текста)
        try:
            get_url = url.replace("{text}", text) if "{text}" in url else url
            params = {"text": text, "voice": voice} if "{text}" not in url else {}
            get_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            resp = httpx.get(get_url, params=params, headers=get_headers, timeout=180)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Кастомный TTS: ошибка запроса: {e}")
    data = resp.content
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype or data[:1] == b"{":
        try:
            j = resp.json()
        except Exception:
            swallow.note('speech_engine._synth_custom:L290')
            j = None
        if isinstance(j, dict):
            for key in ("audio", "audio_base64", "data", "wav", "content"):
                if key in j and isinstance(j[key], str) and len(j[key]) > 64:
                    try:
                        data = base64.b64decode(j[key])
                        break
                    except Exception:
                        swallow.note('speech_engine._synth_custom:L299')
                        pass
            if isinstance(j, dict) and j.get("url") and not data.startswith(b"RIFF"):
                try:
                    import httpx as _hx
                    data = _hx.get(j["url"], timeout=120).content
                except Exception:
                    swallow.note('speech_engine._synth_custom:L306')
                    pass
    if not data:
        raise RuntimeError("Кастомный TTS: пустой ответ")
    if not (data[:4] in (b"RIFF", b"ID3\x03", b"OggS", b"fLaC", b"\xff\xfb", b"MP3\x01")):
        raise RuntimeError("Кастомный TTS: ответ не похож на аудио")
    return _to_wav(data)


def _to_wav(data: bytes) -> bytes:
    """Привести mp3/ogg/прочие форматы к wav (через ffmpeg), иначе вернуть как есть."""
    if data[:4] == b"RIFF":
        return data
    import subprocess
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    try:
        with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as fi:
            fi.write(data)
            inp = fi.name
        outp = inp + ".wav"
        r = subprocess.run([ffmpeg, "-y", "-i", inp, outp],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.exists(outp) and os.path.getsize(outp) > 44:
            with open(outp, "rb") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"[TTS] конвертация в wav не удалась: {e}")
    finally:
        for _p in (inp, outp):
            try:
                if _p and os.path.exists(_p):
                    os.unlink(_p)
            except Exception:
                swallow.note('speech_engine._to_wav:L339')
                pass
    return data


def _to_float(value, default: float) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        swallow.note('speech_engine._to_float:L350')
        return float(default)


def _tts_rate_pitch(rate, pitch):
    """Скорость и тон синтеза: параметр запроса -> настройка профиля.

    До этого speech.tts.rate и speech.tts.pitch из настроек не применялись
    нигде: синтез шёл с rate=1.0/pitch=0.0, если клиент сам их не передавал.
    """
    tts_cfg = get_speech_config().get("tts") or {}
    if rate is None:
        rate = tts_cfg.get("rate")
    if pitch is None:
        pitch = tts_cfg.get("pitch")
    return _to_float(rate, 1.0), _to_float(pitch, 0.0)


def _effective_tts_voice(engine: str, speaker: str = None):
    """Фактический голос для движка: параметр запроса -> настройка профиля.

    Возвращает (значение для синтеза, строка-идентичность голоса). Идентичность
    попадает в ключ кэша: если строить ключ по параметру запроса (часто пустому),
    то после смены голоса в настройках тот же текст отдаётся прежним кэшированным
    аудио — вот он и эффект «голос применяется только после перезапуска сервера».
    """
    tts_cfg = get_speech_config().get("tts") or {}
    if engine == "piper":
        voice = (speaker or tts_cfg.get("piper_voice") or "").strip()
        return voice, "piper:" + (voice or "default")
    if engine == "qwen3":
        voice = (speaker or tts_cfg.get("qwen3_speaker") or "").strip()
        url = str(tts_cfg.get("qwen3_url") or "http://127.0.0.1:7860").strip()
        return voice, f"qwen3:{url}:{voice or 'default'}"
    if engine == "webspeech":
        # Синтез в браузере: голосом управляет сам клиент.
        return "", "browser"
    if engine.startswith("custom:"):
        import hashlib
        import json
        eng_id = engine[len("custom:"):]
        for e in get_speech_config().get("custom_tts_engines") or []:
            if isinstance(e, dict) and e.get("id") == eng_id:
                blob = json.dumps(e, sort_keys=True, ensure_ascii=False)
                return "", "custom:%s:%s" % (eng_id, hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10])
        return "", "custom:%s:missing" % eng_id
    # silero (и неизвестный движок — трактуются так же)
    voice = (speaker or tts_cfg.get("silero_speaker") or "xenia").strip()
    try:
        model_ver = str(getattr(_server(), "SILERO_MODEL_VERSION", "") or "")
    except Exception:
        swallow.note('speech_engine._effective_tts_voice:L401')
        model_ver = ""
    return voice, "silero:%s:%s" % (model_ver or "model", voice)


def _tts_cache_key(text, engine, voice_id, rate, pitch) -> str:
    """Ключ кэша: движок + ФАКТИЧЕСКИЙ голос + скорость + тон + текст.

    voice_id — идентичность из _effective_tts_voice(): имя голоса и версия модели,
    уже разрешённые из конфига, а не сырой параметр запроса.
    """
    import hashlib
    return (f"tts:{engine}:{voice_id or '*'}:{rate}:{pitch}:"
            f"{hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]}")


def synthesize_tts(text: str, engine: str = None, speaker: str = None,
                   rate: float = None, pitch: float = None) -> bytes:
    """Синтезировать текст в WAV-байты выбранным движком.

    engine: silero | piper | qwen3 | webspeech | custom:<id> | (auto — из конфига)
    speaker/voice: голос из запроса; если не задан — берётся из настроек
    речи (speech.tts.silero_speaker / piper_voice / qwen3_speaker).
    rate/pitch: None — значит клиент их не просил, тогда берём speech.tts.rate
    и speech.tts.pitch из настроек.
    Возвращает bytes (wav). Для webspeech возвращается b"" (синтез в браузере).
    Если запрошенный движок недоступен в системе — мягкий фолбэк на лучший
    доступный (auto), затем на webspeech; hard 500 только если ничего не вышло.

    Результат кэшируется здесь, а не в HTTP-обработчике: кэш нужен всем
    вызывающим (голосовой путь агента /api/voice/tts и потоковый
    /api/voice/chat-stream его не имели), а ключ строится по уже разрешённым
    движку и голосу — иначе смена голоса в настройках осталась бы невидимой
    до истечения TTL или до перезапуска сервера. Пустой результат (webspeech —
    синтез в браузере) не кэшируется.
    """
    engine = resolve_tts_engine(engine)
    cfg = get_speech_config()
    tts_cfg = cfg.get("tts") or {}
    if not text:
        raise RuntimeError("Нет текста для синтеза")
    rate, pitch = _tts_rate_pitch(rate, pitch)

    try:
        from cache_manager import tts_cache
    except Exception:
        swallow.note('speech_engine.synthesize_tts:L447')
        tts_cache = None

    def _try(eng: str, voice: str):
        if eng == "silero":
            return _synth_silero(text, voice, rate, pitch)
        if eng == "piper":
            return _synth_piper(text, voice, rate, pitch)
        if eng == "qwen3":
            url = tts_cfg.get("qwen3_url") or "http://127.0.0.1:7860"
            return _synth_qwen3(text, voice, url=url)
        if eng == "webspeech":
            return b""
        if eng.startswith("custom:"):
            eng_id = eng[len("custom:"):]
            for e in cfg.get("custom_tts_engines") or []:
                if isinstance(e, dict) and e.get("id") == eng_id:
                    return _synth_custom(text, e)
            raise RuntimeError(f"Кастомный TTS-движок '{eng_id}' не найден")
        raise RuntimeError(f"Неизвестный TTS-движок: {eng}")

    def _unavailable(exc: Exception) -> bool:
        m = str(exc).lower()
        # Смерть worker-процесса пула Silero — тоже «движок недоступен»,
        # иначе нет фолбэка на piper/qwen3/webspeech.
        if ("brokenprocesspool" in m or "process pool" in m
                or "child process" in m or "пул сломан" in m):
            return True
        return any(k in m for k in (
            "недоступен", "не доступен", "не установлен", "не загружен",
            "не найден", "unavailable", "not installed", "no module",
            "пусто", "пустой", "не вернул", "нет голоса", "голос не",
            "не инициализирован",
        ))

    attempted = {engine}
    last_err = None
    while True:
        # Ключ считаем здесь, а не один раз до цикла: при фолбэке на другой
        # движок или другой голос нельзя сохранять его аудио под ключом
        # первоначально запрошенного.
        voice, voice_id = _effective_tts_voice(engine, speaker)
        cache_key = _tts_cache_key(text, engine, voice_id, rate, pitch)
        if tts_cache is not None:
            hit = tts_cache.get(cache_key)
            if hit:
                return hit
        try:
            data = _try(engine, voice)
            if data and tts_cache is not None:
                tts_cache.set(cache_key, data, ttl=300)
            return data
        except Exception as e:
            last_err = e
            if not _unavailable(e):
                raise
            # фоллбэк: auto → webspeech (или любой ещё не пробованный доступный)
            if engine != "auto":
                fallback = resolve_tts_engine("auto")
            else:
                fallback = "webspeech"
            if fallback in attempted:
                fallback = "webspeech"
            if fallback in attempted:
                raise RuntimeError(f"Не удалось синтезировать речь: {last_err}")
            attempted.add(fallback)
            engine = fallback


# ---------------------------------------------------------------- STT

def resolve_stt_engine(preferred: str = None) -> str:
    """Фактический STT-движок: preferred -> конфиг stt_engine."""
    cfg = get_speech_config()
    return preferred or cfg.get("stt_engine") or "local"


def list_custom_stt():
    cfg = get_speech_config()
    out = []
    for eng in cfg.get("custom_stt_engines") or []:
        if not isinstance(eng, dict):
            continue
        out.append({
            "id": eng.get("id", ""),
            "name": eng.get("name", eng.get("id", "Кастомный STT")),
            "url": eng.get("url", ""),
            "model": eng.get("model", ""),
            "has_key": bool(eng.get("api_key")),
        })
    return out


def _transcribe_api(audio_bytes: bytes, engine: dict, language: str = None) -> dict:
    """Распознавание через OpenAI-совместимый API (multipart)."""
    import httpx
    url = (engine.get("url") or "").strip()
    if not url:
        raise RuntimeError("STT API: не указан URL")
    base = url.rstrip("/")
    if not base.endswith("/audio/transcriptions"):
        base += "/audio/transcriptions"
    api_key = engine.get("api_key") or ""
    model = engine.get("model") or ""
    headers = {}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    files = {"file": ("audio.webm", audio_bytes, "application/octet-stream")}
    data = {}
    if model:
        data["model"] = model
    if language:
        data["language"] = language
    try:
        resp = httpx.post(base, headers=headers, files=files, data=data, timeout=300)
    except Exception as e:
        raise RuntimeError(f"STT API: ошибка запроса: {e}")
    if resp.status_code >= 400:
        raise RuntimeError(f"STT API: HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        j = resp.json()
    except Exception:
        raise RuntimeError(f"STT API: не-json ответ: {resp.text[:200]}")
    text = ""
    if isinstance(j, dict):
        text = j.get("text") or j.get("transcript") or ""
        segments = j.get("segments") or []
    elif isinstance(j, str):
        text = j
        segments = []
    else:
        text = str(j)
    if not text:
        raise RuntimeError("STT API: пустой результат распознавания")
    return {
        "text": text.strip(),
        "language": language or "",
        "duration": 0,
        "segments": segments,
        "words": [],
        "engine": engine.get("id", "api"),
    }


def _transcribe_local(audio_bytes: bytes, language: str = None,
                      vad_filter: bool = True, model: str = None) -> dict:
    """Распознавание через локальный faster-whisper.

    Модель не задана здесь намеренно: её выбирает stt_engine по профилю из
    config.json и по устройству (на CPU по умолчанию base, на CUDA — turbo).
    Раньше тут стоял явный ``ensure_model("turbo")``, из-за чего настройка модели
    в интерфейсе не работала, а на CPU фраза в 3 секунды читалась 16 секунд.

    vad_filter=False — полное распознавание без VAD: обрабатываются все
    30-секундные окна файла (музыка/длинные записи), включается
    температурный fallback, отключается выбрасывание «тихих» окон.
    """
    S = _server()
    if not S.STT_AVAILABLE:
        raise RuntimeError("faster-whisper не установлен. pip install faster-whisper")
    try:
        # Явно запрошенная модель (из интерфейса или ?model=) выигрывает у профиля.
        S.STT.ensure_model(model)
    except Exception as _me:
        logging.getLogger("infinity").warning(f"[STT] ensure_model error: {_me}")
    cfg = get_speech_config()
    stt_cfg = cfg.get("stt") or {}
    lang = language or stt_cfg.get("local_language") or "ru"
    result = S.STT.transcribe_bytes(
        audio_bytes,
        language=lang,
        suffix=".webm",
        vad_filter=vad_filter,
        no_speech_threshold=(0.3 if vad_filter else None),
    )
    result["engine"] = "local"
    return result


def _add_voice_intent(result: dict) -> None:
    """Дописать в результат распознавания намерение (laya), если движок доступен.

    Дополнительный ключ 'intent' — контракт ответа не меняется, старые
    потребители его просто не видят.
    """
    try:
        import laya_client
    except Exception:
        swallow.note('speech_engine._add_voice_intent:L635')
        return
    try:
        if not laya_client.feature("voice_intent"):
            return
        text = (result or {}).get("text") or ""
        if not text.strip():
            return
        intent = laya_client.classify_voice(text)
        if intent:
            result["intent"] = intent
    except Exception as e:
        logging.getLogger("infinity").debug(f"[laya] voice intent failed: {e}")


def transcribe_stt(audio_bytes: bytes, language: str = None, engine: str = None,
                   vad_filter: bool = True, model: str = None) -> dict:
    """Распознавание выбранным движком (local | openai | custom:<id>).

    model - явная модель перевода: пусто = перевод по настройкам.
    """
    result = _transcribe_stt_core(audio_bytes, language, engine, vad_filter, model)
    _add_voice_intent(result)
    return result


def _transcribe_stt_core(audio_bytes: bytes, language: str = None, engine: str = None,
                         vad_filter: bool = True, model: str = None) -> dict:
    """Распознавание выбранным движком (local | openai | custom:<id>)."""
    engine = resolve_stt_engine(engine)
    cfg = get_speech_config()
    # «Web Speech» распознаёт только браузер; серверный запрос с таким
    # значением раньше падал с «Неизвестный STT-движок». Если настройка
    # выбрана, а клиент всё же пришёл на сервер — работаем локальной моделью.
    if engine == "web":
        engine = "local"
    if engine == "local":
        return _transcribe_local(audio_bytes, language, vad_filter=vad_filter,
                                 model=model)
    if engine == "openai":
        stt_cfg = cfg.get("stt") or {}
        api_key = stt_cfg.get("openai_api_key") or ""
        if not api_key:
            raise RuntimeError("OpenAI STT: не задан API-ключ в настройках")
        return _transcribe_api(audio_bytes, {
            "id": "openai",
            "url": stt_cfg.get("openai_base_url") or "https://api.openai.com/v1",
            "api_key": api_key,
            "model": stt_cfg.get("openai_model") or "whisper-1",
        }, language)
    if engine.startswith("custom:"):
        eng_id = engine[len("custom:"):]
        for eng in cfg.get("custom_stt_engines") or []:
            if isinstance(eng, dict) and eng.get("id") == eng_id:
                return _transcribe_api(audio_bytes, eng, language)
        raise RuntimeError(f"Кастомный STT-движок '{eng_id}' не найден")
    raise RuntimeError(f"Неизвестный STT-движок: {engine}")


# ---------------------------------------------------------------- Status / Test

def list_engines() -> dict:
    """Статус всех движков для вкладки настроек и клиентов."""
    S = _server()
    cfg = get_speech_config()
    tts_cfg = cfg.get("tts") or {}
    stt_cfg = cfg.get("stt") or {}

    silero_speakers = []
    try:
        if S.SILERO_TTS_AVAILABLE and hasattr(S, "_list_silero_speakers"):
            silero_speakers = list(S._list_silero_speakers() or [])
    except Exception:
        swallow.note('speech_engine.list_engines:L708')
        pass
    piper_voices = []
    try:
        if hasattr(S, "_list_piper_voices"):
            piper_voices = list(S._list_piper_voices() or [])
    except Exception:
        swallow.note('speech_engine.list_engines:L715')
        pass

    selected_tts = cfg.get("tts_engine") or "auto"
    effective_tts = selected_tts
    try:
        if selected_tts == "auto":
            effective_tts = resolve_tts_engine("auto")
    except Exception:
        swallow.note('speech_engine.list_engines:L724')
        pass

    stt_status = {}
    try:
        if S.STT_AVAILABLE:
            stt_status = S.STT.status()
    except Exception:
        swallow.note('speech_engine.list_engines:L732')
        pass

    return {
        "tts": {
            "selected": selected_tts,
            "effective": effective_tts,
            "engines": {
                "silero": {"available": S.SILERO_TTS_AVAILABLE,
                           "version": getattr(S, "SILERO_MODEL_VERSION", ""),
                           "speakers": silero_speakers},
                "piper": {"available": S.PIPER_TTS_AVAILABLE,
                          "voices": piper_voices},
                "qwen3": {"available": S.QWEN3_TTS_AVAILABLE,
                          "url": tts_cfg.get("qwen3_url") or getattr(S, "QWEN3_URL", ""),
                          "speakers": list(getattr(S, "QWEN3_SPEAKERS", []) or [])},
                "webspeech": {"available": True},
            },
            "custom": list_custom_tts(),
        },
        "stt": {
            "selected": cfg.get("stt_engine") or "local",
            "engines": {
                "local": {
                    "available": S.STT_AVAILABLE,
                    "model": stt_status.get("model") or stt_cfg.get("local_model"),
                    "loaded": stt_status.get("loaded", False),
                    "language": stt_status.get("language") or stt_cfg.get("local_language", "ru"),
                    "device": stt_status.get("device"),
                    "models_dir": stt_status.get("models_dir"),
                    "error": stt_status.get("error"),
                },
                "openai": {
                    "configured": bool(stt_cfg.get("openai_api_key")),
                    "base_url": stt_cfg.get("openai_base_url") or "https://api.openai.com/v1",
                    "model": stt_cfg.get("openai_model") or "whisper-1",
                    "has_key": bool(stt_cfg.get("openai_api_key")),
                },
                "web": {"available": True},
            },
            "custom": list_custom_stt(),
        },
        "enabled": {
            "selection_tts": bool(cfg.get("selection_tts_enabled", True)),
            "voice_input": bool(cfg.get("voice_input_enabled", True)),
        },
    }


def _silent_wav(duration: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Тихий WAV для проверки STT-API (HTTP-доступность)."""
    import struct
    import wave
    n = int(duration * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<%dh" % n, *([0] * n)))
    return buf.getvalue()


def test_engine(engine_type: str, engine_id: str = None, config: dict = None) -> dict:
    """Проверить движок (кнопка «Тест» в настройках).

    engine_type: "tts" | "stt"
    engine_id:   silero | piper | qwen3 | webspeech | local | openai | custom:<id>
    config:      опциональные параметры для временной проверки (url/api_key/...)
    """
    engine_type = (engine_type or "tts").lower()
    if engine_type == "tts":
        return _test_tts(engine_id, config)
    return _test_stt(engine_id, config)


def _test_tts(engine_id: str = None, config: dict = None) -> dict:
    cfg = get_speech_config()
    if not engine_id or engine_id in ("auto", "default"):
        engine_id = resolve_tts_engine("auto")
    text = "Тестовая озвучка. Проверка связи."
    try:
        if engine_id.startswith("custom:"):
            eng_id = engine_id[len("custom:"):]
            eng = None
            for e in cfg.get("custom_tts_engines") or []:
                if isinstance(e, dict) and e.get("id") == eng_id:
                    eng = dict(e)
                    break
            if eng is None and config:
                eng = dict(config)
            if eng is None:
                return {"ok": False, "error": f"Кастомный TTS '{eng_id}' не найден"}
            if config:
                eng.update({k: v for k, v in config.items() if v})
            data = _synth_custom(text, eng)
        elif engine_id == "silero":
            data = _synth_silero(text, (config or {}).get("speaker") or None)
        elif engine_id == "piper":
            data = _synth_piper(text, (config or {}).get("voice") or None)
        elif engine_id == "qwen3":
            data = _synth_qwen3(text, (config or {}).get("speaker") or None,
                                url=(config or {}).get("url") or None)
        elif engine_id == "webspeech":
            return {"ok": True, "engine": engine_id,
                    "message": "Web Speech синтезируется в браузере"}
        else:
            return {"ok": False, "error": f"Неизвестный TTS-движок: {engine_id}"}
        duration = _wav_duration(data)
        return {"ok": True, "engine": engine_id, "bytes": len(data),
                "duration": round(duration, 2)}
    except Exception as e:
        logger.warning(f"[speech] TTS test {engine_id} failed: {e}")
        return {"ok": False, "engine": engine_id, "error": str(e)}


def _test_stt(engine_id: str = None, config: dict = None) -> dict:
    cfg = get_speech_config()
    engine_id = engine_id or resolve_stt_engine("auto")
    wav = _silent_wav()
    try:
        if engine_id == "local":
            S = _server()
            if not S.STT_AVAILABLE:
                return {"ok": False, "error": "faster-whisper не установлен"}
            st = S.STT.status()
            return {"ok": True, "engine": "local",
                    "message": f"Движок доступен. Модель: {st.get('model') or 'не загружена'}, "
                               f"загружена: {st.get('loaded')}"}
        if engine_id == "openai":
            stt_cfg = cfg.get("stt") or {}
            api_key = stt_cfg.get("openai_api_key") or ((config or {}).get("api_key") or "")
            if not api_key:
                return {"ok": False, "error": "Не задан API-ключ"}
            result = _transcribe_api(wav, {
                "id": "openai",
                "url": (config or {}).get("url") or stt_cfg.get("openai_base_url") or "https://api.openai.com/v1",
                "api_key": api_key,
                "model": (config or {}).get("model") or stt_cfg.get("openai_model") or "whisper-1",
            }, "ru")
            return {"ok": True, "engine": "openai", "text": result.get("text", "")[:120]}
        if engine_id.startswith("custom:"):
            eng_id = engine_id[len("custom:"):]
            eng = None
            for e in cfg.get("custom_stt_engines") or []:
                if isinstance(e, dict) and e.get("id") == eng_id:
                    eng = dict(e)
                    break
            if eng is None and config:
                eng = dict(config)
            if eng is None:
                return {"ok": False, "error": f"Кастомный STT '{eng_id}' не найден"}
            if config:
                eng.update({k: v for k, v in config.items() if v})
            result = _transcribe_api(wav, eng, "ru")
            return {"ok": True, "engine": engine_id, "text": result.get("text", "")[:120]}
        if engine_id == "web":
            return {"ok": True, "engine": "web",
                    "message": "Web Speech распознаётся в браузере"}
        return {"ok": False, "error": f"Неизвестный STT-движок: {engine_id}"}
    except Exception as e:
        logger.warning(f"[speech] STT test {engine_id} failed: {e}")
        return {"ok": False, "engine": engine_id, "error": str(e)}


def _wav_duration(wav_bytes: bytes) -> float:
    try:
        import wave
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        swallow.note('speech_engine._wav_duration:L903')
        return 0.0
