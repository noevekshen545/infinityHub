"""Движки синтеза речи: Piper (голоса, кэш, загрузка модели), Silero v5
(hub-кэш, ленивая загрузка), Qwen3 (проверка порта, gradio-клиент) и порядок
ленивой инициализации с событием готовности.

Перенесено из server_backup.py без изменения тел. Все флаги, модели, кэши и
событие готовности (_TTS_READY_EVENT, _TTS_INIT_LOCK, SILERO_*, QWEN3_*,
PIPER_*) читаются и пишутся через _sb: их взводят и читают непересаженные
части монолита, speech_engine и _warmup_tts_later, а _publish_tts_state
копирует именно эти значения в пакет server. __file__ тоже через _sb:
_get_piper_model_dir/_get_silero_hub_dir ищут models/ рядом с монолитом, а не
рядом с этим модулем.
"""

import json
import os
import sys
import threading

import server_backup as _sb
import swallow

def _get_piper_model_dir():
    """Путь к моделям Piper (поддержка PyInstaller)"""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, "models", "piper")
    else:
        return os.path.join(os.path.dirname(os.path.abspath(_sb.__file__)), "models", "piper")

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
                    swallow.note('tts_engine._list_piper_voices:L86')
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
                        swallow.note('tts_engine._list_piper_voices:L110')
                        pass
                voices.append({
                    "id": voice_id,
                    "name": voice_id,
                    "source": source,
                    "size_mb": size_mb,
                    "description": desc,
                })
        except Exception as e:
            _sb.logger.warning(f"Piper: не удалось прочитать {d}: {e}")
    return voices

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
        if mp_ in _sb._PIPER_FAILED:
            continue
        with _sb._PIPER_VOICE_CACHE_LOCK:
            cached = _sb._PIPER_VOICE_CACHE.get(mp_)
        if cached is not None:
            _sb.PIPER_VOICE = cached
            _sb._PIPER_LOADED_PATH = mp_
            return cached
        try:
            from piper import PiperVoice as _PV
            voice = _PV.load(mp_, config_path=cp_)
        except Exception as e:
            _sb._PIPER_FAILED.add(mp_)
            _sb.logger.error(f"Piper: модель {os.path.basename(mp_)} не загрузилась ({e})"
                         f" — пробуем другой голос")
            continue
        with _sb._PIPER_VOICE_CACHE_LOCK:
            _sb._PIPER_VOICE_CACHE[mp_] = voice
        _sb.PIPER_VOICE = voice  # keep default ref
        _sb._PIPER_LOADED_PATH = mp_
        if voice_id and os.path.basename(mp_) != voice_id + ".onnx":
            _sb.logger.warning(f"Piper: голос '{voice_id}' недоступен, используется "
                           f"{os.path.basename(mp_)}")
        elif not voice_id:
            _sb.logger.info(f"Piper: модель по умолчанию недоступна, используется "
                        f"{os.path.basename(mp_)}")
        return voice
    return None

def _get_silero_hub_dir():
    """Определяем путь к Silero hub кэшу (PyInstaller / переносимая сборка)."""
    # При сборке PyInstaller модель будет в _MEIPASS
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # PyInstaller — модель внутри сборки
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
        swallow.note('tts_engine._get_silero_hub_dir:L190')
        pass
    # Обычный режим — модель в папке проекта
    return os.path.join(os.path.dirname(os.path.abspath(_sb.__file__)), "models", "silero", "silero_hub")

def _load_silero_v5():
    """Загружаем Silero TTS из локального hub кэша.

    Приоритет по версии: v5_6_ru → v5_5_ru → v5_4_ru → v5_3_ru → v5_2_ru → v5_1_ru → v5_ru.
    Чем новее модель — тем лучше качество, поэтому по умолчанию берём самую свежую.
    """
    global SILERO_MODEL, SILERO_TTS_AVAILABLE, SILERO_MODEL_VERSION

    # Определяем путь к hub кэшу (приоритет: локальный в проекте, затем системный)
    hub_dir = None
    if os.path.exists(_sb._SILERO_HUB_DIR):
        hub_dir = _sb._SILERO_HUB_DIR
        _sb.logger.info(f"Silero: используем локальный кэш: {hub_dir}")
    elif os.path.exists(_sb._SILERO_TORCH_HUB_DIR):
        hub_dir = _sb._SILERO_TORCH_HUB_DIR
        _sb.logger.info(f"Silero: используем системный кэш: {hub_dir}")

    if not hub_dir:
        _sb.logger.info("Silero TTS: hub кэш не найден")
        return

    # Список моделей от самой новой к самой старой
    candidates = ["v5_6_ru", "v5_5_ru", "v5_4_ru", "v5_3_ru", "v5_2_ru", "v5_1_ru", "v5_ru"]

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
                _sb.SILERO_MODEL = model
                _sb.SILERO_TTS_AVAILABLE = True
                _sb.SILERO_MODEL_VERSION = speaker_id
                _sb.logger.info(f"Silero TTS {speaker_id} загружен. Спикеры: {model.speakers}")
                return
            except Exception as e_inner:
                _sb.logger.info(f"Silero {speaker_id} недоступна ({e_inner}), пробуем следующую...")
                continue
        _sb.logger.warning("Silero TTS: ни одна из моделей v5_6_ru..v5_ru не загрузилась")
    except Exception as e:
        _sb.logger.warning(f"Silero TTS ошибка загрузки: {e}")

def _init_tts_background():
    """Фоновая загрузка Silero (torch) + silero_pool + Qwen3."""
    global SILERO_MODEL, SILERO_TTS_AVAILABLE, SILERO_MODEL_VERSION
    global _SILERO_POOL, QWEN3_TTS_AVAILABLE, QWEN3_CLIENT, TTS_ENGINE
    try:
        _load_silero_v5()
    except Exception as _e:
        _sb.logger.warning(f"[tts-bg] Silero загрузка: {_e}")
    try:
        import silero_pool as _pool
        _pool.init_pool(_sb._SILERO_HUB_DIR, workers=2)
        _sb._SILERO_POOL = _pool
    except Exception as _e:
        _sb._SILERO_POOL = None
        _sb.logger.warning(f"silero_pool недоступен: {_e}")
    try:
        _init_qwen3()
    except Exception as _e:
        _sb.logger.warning(f"[tts-bg] Qwen3 инициализация: {_e}")
    _sb.TTS_ENGINE = ("silero" if _sb.SILERO_TTS_AVAILABLE else
                  ("qwen3" if _sb.QWEN3_TTS_AVAILABLE else
                   ("piper" if _sb.PIPER_TTS_AVAILABLE else "webspeech")))
    # Сначала публикуем состояние (важно для server.* и UI), потом снимаем
    # флаг готовности — ожидающие потоки увидят уже актуальные значения.
    _publish_tts_state()
    _sb._TTS_READY_EVENT.set()
    _sb.logger.info(f"TTS фоновая инициализация завершена (engine={_sb.TTS_ENGINE})")

def _ensure_tts_ready(timeout=60.0):
    """Дождаться готовности TTS, при необходимости ЛЕНИВО запустив инициализацию.

    Раньше инициализация стартовала сама при импорте модуля — это задерживало
    старт на ~12 с из-за загрузки весов Silero через torch. Теперь она
    поднимается здесь, при первом реальном обращении к синтезу речи.
    """
    _start_tts_init_async()
    if not _sb._TTS_READY_EVENT.is_set():
        _sb._TTS_READY_EVENT.wait(timeout)
    # На случай, если пакет `server` импортирован позже — синхронизируем ещё раз.
    _publish_tts_state()
    return True

def _start_tts_init_async():
    """Запустить фоновую инициализацию TTS, если она ещё не запускалась.

    Потокобезопасно: под защитой Lock проверяется флаг, поэтому параллельные
    запросы не создадут два потока загрузки. Возвращает True, если инициализация
    уже идёт или завершилась.
    """
    global _TTS_INIT_STARTED, _TTS_INIT_THREAD
    if _sb._TTS_READY_EVENT.is_set():
        return True
    with _sb._TTS_INIT_LOCK:
        if _sb._TTS_INIT_STARTED:
            return True
        _sb._TTS_INIT_STARTED = True
        _sb._TTS_INIT_THREAD = threading.Thread(
            target=_init_tts_background, daemon=True, name="tts-bg-init")
        _sb._TTS_INIT_THREAD.start()
        _sb.logger.info("TTS: ленивая инициализация запущена по первому обращению")
    return True

def _init_qwen3():
    global QWEN3_TTS_AVAILABLE, QWEN3_CLIENT, QWEN3_SPEERS
    try:
        if not _qwen3_port_open(_sb.QWEN3_URL):
            _sb.QWEN3_TTS_AVAILABLE = False
            _sb.QWEN3_CLIENT = None
            _sb.logger.info(f"Qwen3-TTS не запущен (порт {_sb.QWEN3_URL} не отвечает) — пропуск")
            return
        from gradio_client import Client
        # ИСПРАВЛЕНИЕ 30.09.2026: в gradio_client >= 1.0 параметр `timeout`
        # убран из Client.__init__ (в 2.6 его нет вовсе) — был TypeError и
        # Qwen3-TTS молча выключался. Пробуем варианты по убыванию новизны.
        _sb.QWEN3_CLIENT = None
        for kwargs in ({"httpx_kwargs": {"timeout": 15}}, {"timeout": 15}, {}):
            try:
                _sb.QWEN3_CLIENT = Client(_sb.QWEN3_URL, **kwargs)
                break
            except TypeError:
                swallow.note('tts_engine._init_qwen3:L323')
                continue
        if _sb.QWEN3_CLIENT is None:
            raise RuntimeError("не удалось создать gradio_client.Client")
        _sb.QWEN3_TTS_AVAILABLE = True
        _sb.logger.info(f"Qwen3-TTS доступен на {_sb.QWEN3_URL}")
    except Exception as e:
        _sb.QWEN3_TTS_AVAILABLE = False
        _sb.logger.info(f"Qwen3-TTS не доступен: {e}")

def _qwen3_synthesize(text, speaker=None, language=None, temperature=0.7, top_p=0.9, max_tokens=2048):
    if not _sb.QWEN3_TTS_AVAILABLE or _sb.QWEN3_CLIENT is None:
        raise Exception("Qwen3-TTS не доступен")
    if not speaker:
        speaker = _sb.QWEN3_DEFAULT_SPEAKER
    if not language:
        language = _sb.QWEN3_DEFAULT_LANGUAGE
    result = _sb.QWEN3_CLIENT.predict(
        text, language, speaker, _sb.QWEN3_MODEL_SIZE,
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
            swallow.note('tts_engine._qwen3_synthesize:L353')
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
            swallow.note('tts_engine._apply_silero_rate_pitch:L407')
            try:
                factor = 2.0 ** (pitch / 12.0)
                new_len = max(1, int(len(out) / factor))
                idx = np.linspace(0, len(out) - 1, new_len).astype(np.int64)
                shifted = out[idx]
                # возврат к исходной длине
                idx2 = np.linspace(0, len(shifted) - 1, len(out)).astype(np.int64)
                out = shifted[idx2]
            except Exception:
                swallow.note('tts_engine._apply_silero_rate_pitch:L418')
                pass
        except Exception:
            swallow.note('tts_engine._apply_silero_rate_pitch:L421')
            pass

    # 2) Time stretch (rate) — ресемплинг: большее значение = короче = быстрее
    if abs(rate - 1.0) >= 0.01:
        try:
            target_len = max(1, int(len(out) / rate))
            idx = np.linspace(0, len(out) - 1, target_len).astype(np.int64)
            out = out[idx]
        except Exception:
            swallow.note('tts_engine._apply_silero_rate_pitch:L431')
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
        _sb.logger.warning(f"_apply_silero_rate_pitch_to_wav failed: {e}")
        return wav_bytes

def _publish_tts_state():
    """Скопировать изменившиеся TTS-глобалы в пакет `server`.

    server/__init__.py копирует атрибуты server_backup ПО ЗНАЧЕНИЮ при
    импорте, поэтому ленивая инициализация (Silero/Qwen3) не видна через
    server.*: UI и speech_engine видели SILERO_TTS_AVAILABLE=False. После
    загрузки синхронизируем эти имена, если пакет `server` уже импортирован.
    """
    try:
        import sys as _sys
        _pkg = _sys.modules.get("server")
        if _pkg is None:
            return
        for _n, _v in (
            ("SILERO_TTS_AVAILABLE", _sb.SILERO_TTS_AVAILABLE),
            ("SILERO_MODEL", _sb.SILERO_MODEL),
            ("SILERO_MODEL_VERSION", _sb.SILERO_MODEL_VERSION),
            ("_SILERO_POOL", _sb._SILERO_POOL),
            ("TTS_ENGINE", _sb.TTS_ENGINE),
            ("QWEN3_TTS_AVAILABLE", _sb.QWEN3_TTS_AVAILABLE),
            ("QWEN3_CLIENT", _sb.QWEN3_CLIENT),
        ):
            try:
                setattr(_pkg, _n, _v)
            except Exception:
                swallow.note('tts_engine._publish_tts_state:L495')
                pass
    except Exception:
        swallow.note('tts_engine._publish_tts_state:L498')
        pass

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
        swallow.note('tts_engine._qwen3_port_open:L517')
        return False
