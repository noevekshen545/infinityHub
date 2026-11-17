"""Voice synthesis and transcription for Infinity frontend.

Единый источник голоса — движок самого приложения (Silero/piper + faster-whisper),
доступный через адаптеры `/api/voice/tts` и `/api/voice/stt`
(см. `integration/voice_bridge.py`). Раньше агент держал вторую копию
Silero + faster-whisper: модели занимали сотни МБ и ставились отдельно
(в том числе лениво, через ensure("stt.faster_whisper")).

Локальные модели остались как аварийный вариант и включаются только явно:
    INFINITY_AGENT_LOCAL_VOICE=1
Если системный движок недоступен (приложение не запущено) — используется он.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import threading
import uuid
import wave
from pathlib import Path
from typing import Optional
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

logger = logging.getLogger("infinity.voice")

_REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.getenv("INFINITY_STT_DIR") or (_REPO_ROOT / "models"))

# Аварийный режим: собственные модели агента вместо движка системы.
USE_LOCAL_VOICE = os.getenv("INFINITY_AGENT_LOCAL_VOICE", "").strip().lower() in (
    "1", "true", "yes", "on")


def _app_voice_cfg():
    """(root, mount) движка приложения: root='http://127.0.0.1:<port>', mount='/agentai'.

    Адрес строится напрямую из app/config.json, без импорта пакета integration:
    в замороженном HermesAgent.exe он может не импортироваться, из-за чего агент
    терял связь с движком InfinitySearch и падал на отсутствующие локальные модели
    (faster-whisper/torch намеренно не входят в сборку агента).
    """
    port = None
    env_port = os.environ.get("INFINITY_MAIN_PORT")
    if env_port and env_port.isdigit():
        port = int(env_port)
    mount = (os.environ.get("AGENTAI_INT_MOUNT")
             or os.environ.get("HERMES_INT_MOUNT") or "")
    if not port or not mount:
        try:
            cfg_path = _REPO_ROOT.parent / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if not port:
                p = (cfg.get("server") or {}).get("port")
                if p:
                    port = int(p)
            if not mount:
                mount = str((cfg.get("agentai") or {}).get("mount") or "")
        except Exception as e:  # noqa: BLE001
            logger.debug("config.json для голоса не читается: %s", e)
    if not port:
        port = 8181
    if not mount:
        mount = "/agentai"
    return "http://127.0.0.1:%d" % port, mount.rstrip("/")


def _system_voice(kind: str) -> Optional[str]:
    """URL адаптера системного голоса ('tts'/'stt') либо None."""
    if USE_LOCAL_VOICE:
        return None
    root, mount = _app_voice_cfg()
    return root + mount + ("/api/voice/tts" if kind == "tts" else "/api/voice/stt")


def _post_multipart(url: str, fields=None, files=None, timeout: int = 180):
    """POST multipart без внешних зависимостей → (status, body)."""
    import urllib.request

    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (fields or {}).items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, key, value)).encode("utf-8"))
    for key, (filename, data, ctype) in (files or {}).items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                      'Content-Type: %s\r\n\r\n' % (boundary, key, filename, ctype)
                      ).encode("utf-8") + data + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    req = urllib.request.Request(
        url, data=b"".join(parts),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _system_tts(text: str) -> Optional[bytes]:
    """Синтез на движке системы. None — движок недоступен или ответ пустой."""
    url = _system_voice("tts")
    if not url:
        return None
    try:
        status, body = _post_multipart(url, fields={"input": text}, timeout=180)
        if status == 204 or not body:
            return None
        return body
    except Exception as e:  # noqa: BLE001
        logger.warning("Системный TTS недоступен (%s) — пробуем локальный", e)
        return None


def _system_stt(audio_bytes: bytes, lang: str = "ru",
                model: Optional[str] = None) -> Optional[str]:
    """Распознавание на движке системы. None — недоступно.

    model (необязательный) уходит в поле ``model`` multipart: так выбор модели
    в интерфейсе доходит до Whisper. Пустое значение — «Авто»: движок системы
    сам выбирает модель по профилю и устройству.
    """
    url = _system_voice("stt")
    if not url:
        return None
    fields = {"language": _clean_lang(lang) or "ru"}
    if model:
        fields["model"] = str(model)
    try:
        status, body = _post_multipart(
            url, fields=fields,
            files={"file": ("audio.wav", audio_bytes, "audio/wav")}, timeout=300)
        if status != 200:
            return None
        data = json.loads(body.decode("utf-8", "replace"))
        text = (data.get("text") or "").strip()
        return text or None
    except Exception as e:  # noqa: BLE001
        logger.warning("Системный STT недоступен (%s) — пробуем локальный", e)
        return None


def _system_stt_status() -> dict:
    root, _mount = _app_voice_cfg()
    url = root + "/api/stt/status"
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return {"ok": True, "status": json.loads(r.read().decode("utf-8", "replace"))}
    except Exception as e:  # noqa: BLE001
        swallow.note('voice._system_stt_status:L159', e)
        return {"ok": False, "error": str(e)[:120]}

# «имя модели» → (репозиторий HuggingFace, папки-кандидаты внутри models/)
# Первая папка — каноническая (её создаёт download_voice_models.py), остальные —
# короткие имена, под которыми веса кладут другие сборки/проекты (base, turbo, ...).
# Плюс всегда проверяется кэш HuggingFace: models--<org>--<repo>/snapshots/<rev>/.
STT_REPOS = {
    "base": ("Systran/faster-whisper-base",
             ("faster-whisper-base", "base")),
    "small": ("Systran/faster-whisper-small",
              ("faster-whisper-small", "small")),
    "medium": ("Systran/faster-whisper-medium",
               ("faster-whisper-medium", "medium")),
    "large-v3-turbo": ("deepdml/faster-whisper-large-v3-turbo-ct2",
                       ("faster-whisper-large-v3-turbo", "large-v3-turbo", "turbo")),
}
DEFAULT_STT_MODEL = os.getenv("INFINITY_STT_MODEL", "small")

# --- Состояние моделей (ленивая загрузка, кэш по имени) ---
_stt_lock = threading.Lock()
_stt_models: dict = {}                 # имя модели → загруженный объект
_stt_failed: dict = {}                 # имя модели → причина отказа
_stt_engine: Optional[str] = None      # 'faster-whisper' | 'openai-whisper' | 'failed'
_stt_name: Optional[str] = None        # активная (последняя запрошенная) модель
_stt_error: Optional[str] = None

_silero_model = None
_silero_speaker = "xenia"


def _cpu_threads() -> int:
    """Whisper на CPU: половина ядер — остальное оставляем шлюзу и модели LLM."""
    override = os.getenv("INFINITY_STT_THREADS")
    if override and override.isdigit():
        return max(1, int(override))
    cores = os.cpu_count() or 4
    return max(1, min(16, cores // 2))


def _model_candidates(name: str) -> list:
    """Папки в ``models/``, где могут лежать веса этой модели (в порядке приоритета)."""
    entry = STT_REPOS.get(name)
    if not entry:
        return []
    folders = entry[1]
    out = [MODELS_DIR / f for f in folders]
    # Кэш HuggingFace: models--<org>--<repo>[/snapshots/<rev>]
    out.append(MODELS_DIR / ("models--" + entry[0].replace("/", "--")))
    return out


def _has_weights(folder: Path) -> bool:
    try:
        return (folder / "model.bin").is_file()
    except OSError:
        swallow.note('voice._has_weights:L215')
        return False


def _local_model_path(name: str) -> Optional[Path]:
    """Локальная папка модели в ``models/`` — приоритетный (офлайн) источник."""
    for cand in _model_candidates(name):
        if _has_weights(cand):
            return cand
        snapshots = cand / "snapshots"
        try:
            if snapshots.is_dir():
                for snap in sorted(snapshots.iterdir(), reverse=True):
                    if _has_weights(snap):
                        return snap
        except OSError:
            swallow.note('voice._local_model_path:L231')
            continue
    return None


def _local_models() -> dict:
    """Найденные на диске локальные модели: имя → папка с весами."""
    found = {}
    for name in sorted(STT_REPOS):
        path = _local_model_path(name)
        if path:
            found[name] = str(path)
    return found


def _load_stt_ex(name: Optional[str] = None):
    """Ленивая загрузка STT по имени: faster-whisper → openai-whisper.

    Возвращает ``(модель, движок)`` или ``(None, None)``. Движок нужен вызывающему:
    у faster-whisper и openai-whisper разные сигнатуры ``transcribe``.
    """
    global _stt_engine, _stt_name, _stt_error
    want = str(name or DEFAULT_STT_MODEL or "small").strip() or "small"
    cached = _stt_models.get(want)
    if cached is not None:
        return cached, _stt_engine
    if want in _stt_failed:
        return None, None
    with _stt_lock:
        cached = _stt_models.get(want)
        if cached is not None:
            return cached, _stt_engine
        if want in _stt_failed:
            return None, None
        local = _local_model_path(want)
        target = str(local) if local else want
        fw_error = None
        try:
            from faster_whisper import WhisperModel
            logger.info("Загрузка faster-whisper (%s): CPU, int8, threads=%d", target, _cpu_threads())
            model = WhisperModel(target, device="cpu", compute_type="int8",
                                 cpu_threads=_cpu_threads(), num_workers=1)
            _stt_models.clear()                 # держим только активную модель (RAM/CPU)
            _stt_models[want] = model
            _stt_engine, _stt_name, _stt_error = "faster-whisper", want, None
            logger.info("faster-whisper готова: %s (%s)", want, target)
            return model, _stt_engine
        except Exception as e:  # noqa: BLE001 — любой сбой → фолбэк на openai-whisper
            fw_error = e
            logger.warning("faster-whisper недоступна для %s (%s) — пробую openai-whisper", want, e)
        try:
            import whisper as _ow
            ow_name = want if want in ("base", "small", "medium", "large-v3", "turbo") else "base"
            logger.info("Загрузка openai-whisper (%s)", ow_name)
            model = _ow.load_model(ow_name)
            _stt_models.clear()
            _stt_models[want] = model
            _stt_engine, _stt_name, _stt_error = "openai-whisper", ow_name, None
            return model, _stt_engine
        except Exception as e:  # noqa: BLE001
            reason = f"faster-whisper: {fw_error}; openai-whisper: {e}"
            _stt_failed[want] = reason
            _stt_engine, _stt_name, _stt_error = "failed", want, reason
            logger.error("STT недоступна (%s): %s", want, reason)
            return None, None


def _load_stt(name: Optional[str] = None):
    """Совместимая обёртка: только модель (см. :func:`_load_stt_ex`)."""
    return _load_stt_ex(name)[0]

def _decode_wav(data: bytes):
    """WAV bytes → numpy float32 mono 16 кГц. ValueError на неподдерживаемом формате."""
    import numpy as np

    with wave.open(io.BytesIO(data), "rb") as w:
        channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    if width == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    elif width == 1:
        arr = (np.frombuffer(raw, dtype="u1").astype("float32") - 128.0) / 128.0
    elif width == 4:
        arr = np.frombuffer(raw, dtype="<i4").astype("float32") / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {width}")
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    if rate and rate != 16000 and arr.size > 1:
        # линейная передискретизация до 16 кГц (Whisper работает на 16 кГц)
        n = int(round(arr.size * 16000.0 / rate))
        arr = np.interp(np.linspace(0, arr.size - 1, n), np.arange(arr.size), arr).astype("float32")
    return arr


def _clean_lang(lang: Optional[str]) -> Optional[str]:
    """Привести код языка к 2-буквенному ISO (ru-RU -> ru, en-US -> en).
    faster-whisper не принимает региональные суффиксы."""
    if not lang:
        return None
    cleaned = str(lang).split("-")[0].split("_")[0].strip().lower()
    return cleaned if len(cleaned) in (2, 3) else None


def _transcribe_sync(audio_bytes: bytes, lang: str = "ru", model_name: Optional[str] = None) -> str:
    """Синхронная транскрибация (выполняется в отдельном потоке)."""
    lang = _clean_lang(lang)
    model, engine = _load_stt_ex(model_name)
    if model is None:
        want = str(model_name or DEFAULT_STT_MODEL or "small")
        raise RuntimeError(_stt_failed.get(want) or _stt_error or
                           "STT-модель не загружена — запустите download-voice-models.bat")

    if engine == "faster-whisper":
        try:
            audio = _decode_wav(audio_bytes)
        except Exception:  # noqa: BLE001 — не WAV (webm/ogg) — отдаём декодеру faster-whisper
            swallow.note('voice._transcribe_sync:L348')
            audio = io.BytesIO(audio_bytes)
        segments, _info = model.transcribe(
            audio, language=lang or None, beam_size=1, best_of=1, temperature=0.0,
            vad_filter=False, condition_on_previous_text=False,
            without_timestamps=True, word_timestamps=False,
            no_speech_threshold=0.6, log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4)
        return " ".join(seg.text.strip() for seg in segments).strip()

    # Фолбэк: openai-whisper (принимает numpy float32 16 кГц)
    import numpy as np
    audio = _decode_wav(audio_bytes)
    result = model.transcribe(np.asarray(audio, dtype="float32"), language=lang or None,
                              fp16=False, condition_on_previous_text=False)
    return (result.get("text") or "").strip()


async def transcribe(audio_bytes: bytes, lang: str = "ru", model: Optional[str] = None) -> str:
    """Transcribe audio to text.

    Основной путь — движок системы (тот же faster-whisper, что и в /desktop).
    ``model`` используется только в аварийном локальном режиме.
    """
    if not USE_LOCAL_VOICE:
        text = await asyncio.to_thread(_system_stt, audio_bytes, lang or "ru", model)
        if text is not None:
            return text
    try:
        # ct2/torch-инференс синхронный — не блокируем event loop сервера
        return await asyncio.to_thread(_transcribe_sync, audio_bytes, lang, model)
    except Exception as e:  # noqa: BLE001
        logger.exception("Transcription failed")
        return f"[Ошибка распознавания: {e}]"


def stt_status() -> dict:
    """Диагностика STT: системный движок (по умолчанию) либо локальные веса."""
    if not USE_LOCAL_VOICE:
        sys_st = _system_stt_status()
        if sys_st.get("ok"):
            # engine/model остаются от приложения ("local", turbo, …),
            # source показывает, что веса и процесс — из движка системы.
            return {"source": "system", "system": "infinity", **sys_st.get("status", {})}
        return {"source": "system", "system": "infinity", "available": False,
                "error": sys_st.get("error"), "fallback": "local"}
    local = _local_models()
    active = _stt_name or DEFAULT_STT_MODEL
    return {
        "engine": _stt_engine,
        "source": "local",
        "model": active,
        "loaded": _stt_models.get(active) is not None,
        "loaded_models": sorted(_stt_models),
        "error": _stt_error,
        "models_dir": str(MODELS_DIR),
        "local_models": sorted(local),
        "local_paths": local,
        "available": sorted(STT_REPOS),
        "cpu_threads": _cpu_threads(),
    }


async def warmup() -> dict:
    """Прогрев в фоне, чтобы первый голосовой запрос не ждал загрузки."""
    if not USE_LOCAL_VOICE:
        # Системный движок и так уже прогрет самим приложением — просто
        # проверяем, что он отвечает, и не трогаем модели агента.
        return await asyncio.to_thread(stt_status)
    await asyncio.to_thread(_load_stt)
    return stt_status()

def _synthesize_sync(text: str, speaker: str = "xenia") -> Optional[bytes]:
    """Синхронный синтез речи (выполняется в отдельном потоке)."""
    global _silero_model

    import torch
    import torchaudio

    if _silero_model is None:
        logger.info("Loading Silero5.5 TTS model...")
        loaded = torch.hub.load(
            'snakers4/silero-models',
            'silero_tts',
            language='ru',
            speaker='v5_5_ru',  # Silero5.5
            trust_repo=True
        )
        # silero_tts может вернуть (model, ...) — берём первый элемент
        _silero_model = loaded[0] if isinstance(loaded, tuple) else loaded

    # Генерируем аудио с голосом xenia
    audio = _silero_model.apply_tts(
        text=text,
        speaker=speaker,  # xenia
        sample_rate=48000
    )

    # Конвертируем в WAV bytes (вручную — torchaudio.save в новых torch требует torchcodec)
    samples = (audio.squeeze().numpy() * 32767.0).clip(-32768, 32767).astype('int16')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(samples.tobytes())
    return buffer.getvalue()


async def synthesize(text: str, speaker: str = "xenia") -> Optional[bytes]:
    """Синтез речи на движке системы (Silero/piper), без второй копии моделей."""
    if not USE_LOCAL_VOICE:
        data = await asyncio.to_thread(_system_tts, text)
        if data:
            return data
    try:
        # torch-инференс синхронный — не блокируем event loop сервера
        return await asyncio.to_thread(_synthesize_sync, text, speaker)
    except Exception as e:
        logger.exception("TTS synthesis failed")
        return None
