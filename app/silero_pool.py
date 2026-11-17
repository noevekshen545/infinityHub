"""Silero TTS process pool.

Зачем: Silero — синхронный PyTorch, держит GIL и грузит CPU. Один синтез
блокирует весь поток uvicorn, отчего полл аватара / чат / bash начинают
отвечать с задержкой. Здесь мы крутим N отдельных Python-процессов,
каждый со своей копией модели; синтез идёт по-настоящему параллельно
и не трогает основной процесс.

Безопасность: если пул не поднялся, `is_ready()` вернёт False и
вызывающий код должен упасть обратно на старый in-process `SILERO_MODEL`.
"""
import os, sys, io, wave, logging, multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import swallow

logger = logging.getLogger("infinity")

_MODEL = None   # глобал внутри ВОРКЕРА, не основного процесса
_MODEL_VERSION = "v5_6_ru"  # используемая версия (для логирования)
_POOL = None    # сам ProcessPoolExecutor — живёт в основном процессе
_HUB_DIR = None # путь к torch.hub моделям — нужен для пересоздания пула

# Токенсет Silero RU v5 (models.yml): '_~абвгдеёжзийклмнопрстуфхцчшщъыьэюя +.,!?…:;–'
# Ни цифр, ни '-', ни '()', ни '*', ни кавычек модель не знает — apply_tts падает
# с KeyError в preprocess_tacotron (путь с акцентами обходит фильтр модели).
_RU_SYMBOLS_FALLBACK = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя +.,!?…:;–"
_DIGIT_WORDS = {
    "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре",
    "5": "пять", "6": "шесть", "7": "семь", "8": "восемь", "9": "девять",
}

_ALLOWED = None  # набор символов загруженной модели — вычисляется в воркере


def silero_allowed_symbols(model=None):
    """Набор символов, которые принимает модель (из symbol_to_id).

    Достаём динамически — вдруг в другой версии модели токенсет расширили.
    Если модель недоступна — жёсткий fallback для RU v5.
    """
    if model is not None:
        try:
            d = getattr(model, "symbol_to_id", None)
            if isinstance(d, dict) and d:
                return set(d)
            pkgs = getattr(model, "packages", None) or []
            s = set()
            for p in pkgs:
                d = getattr(p, "symbol_to_id", None)
                if isinstance(d, dict):
                    s |= set(d)
            if s:
                return s
        except Exception:
            swallow.note('silero_pool.silero_allowed_symbols:L55')
            pass
    return set(_RU_SYMBOLS_FALLBACK)


def silero_safe_text(text, allowed=None, max_len=None):
    """Чистит текст под токенсет Silero RU, чтобы apply_tts не падал с KeyError.

    - вырезает markdown (** / * / # / бэктики), URL и HTML-теги;
    - дефисы и тире приводит к '–' (ен-даш — единственный поддерживаемый символ);
    - цифры превращает в русские слова ('1–2' -> 'один–два');
    - удаляет всё, чего нет в токенсете модели (скобки, кавычки и т.п.).
    """
    import re as _re
    if not text:
        return ""
    if allowed is None:
        allowed = _RU_SYMBOLS_FALLBACK
    t = str(text)
    t = _re.sub(r"https?://\S+", " ", t)
    t = _re.sub(r"<[^>]+>", " ", t)
    t = _re.sub(r"`{1,3}", "", t)
    t = t.replace("**", " ").replace("*", " ")
    t = t.replace("_", " ").replace("~", " ").replace("#", " ")
    t = t.replace("—", "–").replace("−", "–").replace("-", "–")
    t = t.replace("«", " ").replace("»", " ")
    t = t.replace("“", " ").replace("”", " ").replace("„", " ")
    t = t.replace("'", " ").replace('"', " ")
    for ch in "()[]{}<>|/\\=+$&@%^":
        t = t.replace(ch, " ")
    t = "".join(_DIGIT_WORDS.get(ch, ch) for ch in t)
    t = t.lower()
    t = "".join(c for c in t if c in allowed)
    t = _re.sub(r"\s+", " ", t).strip()
    if max_len:
        t = t[:max_len]
    return t


def _worker_init(hub_dir, speaker_id="v5_6_ru"):
    """Запускается один раз в каждом worker'е при старте.

    Пробуем загрузить самую свежую модель из семейства v5_x_ru (сейчас v5_6_ru),
    откатываясь на более старые, если нужная отсутствует.
    """
    global _MODEL, _MODEL_VERSION, _ALLOWED
    try:
        import torch
        try:
            torch.set_num_threads(2)
        except Exception:
            swallow.note('silero_pool._worker_init:L106')
            pass
        for candidate in ("v5_6_ru", "v5_5_ru", "v5_4_ru", "v5_3_ru", "v5_2_ru", "v5_1_ru", "v5_ru"):
            try:
                _MODEL, _ = torch.hub.load(
                    repo_or_dir=hub_dir,
                    model='silero_tts',
                    language='ru',
                    speaker=candidate,
                    trust_repo=True,
                    source='local',
                )
                _MODEL_VERSION = candidate
                _ALLOWED = silero_allowed_symbols(_MODEL)
                print(f"[silero_pool worker] loaded {candidate}", file=sys.stderr)
                return
            except Exception as e_inner:
                print(f"[silero_pool worker] {candidate} unavailable: {e_inner}", file=sys.stderr)
                continue
        _MODEL = None
        print("[silero_pool worker] no silero TTS model loaded", file=sys.stderr)
    except Exception as e:
        _MODEL = None
        print(f"[silero_pool worker] init failed: {e}", file=sys.stderr)


def _worker_synth(text, speaker, sample_rate):
    """Сам синтез внутри worker'а. Возвращает готовый WAV (bytes)."""
    global _MODEL
    if _MODEL is None:
        raise RuntimeError("Silero модель не загружена в worker")
    text = silero_safe_text(text, _ALLOWED)
    if len(text) < 2:
        raise RuntimeError("Silero: текст пуст после очистки от неподдерживаемых символов")
    if speaker not in (_MODEL.speakers or []):
        # Клиент может прислать идентификатор чужого движка (например,
        # голос Google из настроек аватара). Первый спикер модели — aidar,
        # мужской; молча подставлять его нельзя: пользователь ждёт выбранный
        # женский голос. Поэтому по умолчанию xenia, если он есть.
        speakers = _MODEL.speakers or []
        speaker = "xenia" if "xenia" in speakers else (speakers[0] if speakers else "xenia")
    import torch, numpy as np
    audio = _MODEL.apply_tts(text=text, speaker=speaker, sample_rate=sample_rate)
    samples = (audio.numpy() if isinstance(audio, torch.Tensor) else np.array(audio)).flatten()
    if samples.size == 0:
        raise RuntimeError("Silero вернул пустые сэмплы")
    audio_int16 = (samples * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def _make_spawn_mp_safe():
    """Подменить __main__.__file__ для дочерних процессов spawn.

    Проблема: multiprocessing со start-method 'spawn' перед созданием
    воркера заново исполняет ГЛАВНЫЙ скрипт (main.py) как __mp_main__.
    В main.py на это стоит защитный RuntimeError («запускайте как скрипт»),
    поэтому каждый воркер пула умирал мгновенно, и Silero «отваливалась»
    целиком (BrokenProcessPool навсегда).

    Решение: воркер-функции (_worker_init/_worker_synth) живут здесь, в
    silero_pool.py — самодостаточном модуле без побочных эффектов. Указываем
    spawn'у этот файл вместо main.py: ребёнок исполняет его как __mp_main__
    и спокойно стартует.
    """
    try:
        mm = sys.modules.get("__main__")
        if mm is None:
            return
        mf = getattr(mm, "__file__", None)
        if not mf:
            return
        base = os.path.basename(mf)
        if base in ("main.py", "server.py"):
            here = os.path.abspath(__file__)
            if os.path.abspath(mf) != here:
                mm.__file__ = here
                logger.info(f"silero_pool: spawn __main__ перенаправлен на {here} "
                            f"(было {base})")
    except Exception as e:
        logger.warning(f"silero_pool: _make_spawn_mp_safe: {e}")


def default_workers():
    """Сколько процессов Silero держать наготове.

    Синтез одной фразы занимает ~0.25 с, поэтому два воркера простаивают, пока
    голосовой помощник ждёт следующий запрос, а при разбиении ответа на
    предложения они начинают работать внахлёст. Каждый процесс — это torch и
    веса Silero в RAM (128 ГБ на этой машине), поэтому потолок держим небольшим.
    Настройка — speech.tts.silero_workers; 0 = авто.
    """
    raw = os.environ.get("INFINITY_SILERO_WORKERS", "").strip()
    if not raw.isdigit():
        try:
            from config_module import get_speech
            raw = str(((get_speech() or {}).get("tts") or {}).get("silero_workers") or "").strip()
        except Exception:
            swallow.note('silero_pool.default_workers:L209')
            raw = ""
    if raw.isdigit() and int(raw) > 0:
        return min(8, int(raw))
    return max(2, min(4, (os.cpu_count() or 4) // 8))


def init_pool(hub_dir, workers=None):
    """Поднимает пул. Безопасно: при ошибке вернёт None и пул просто не активен."""
    global _POOL, _HUB_DIR
    if _POOL is not None:
        return _POOL
    if not hub_dir or not os.path.exists(hub_dir):
        logger.info("silero_pool: hub_dir не найден, пул не активен")
        return None
    if workers is None:
        workers = default_workers()
    _HUB_DIR = hub_dir
    _make_spawn_mp_safe()
    try:
        ctx = mp.get_context("spawn")  # spawn — чтобы не унаследовать GIL/torch состояние
        _POOL = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=ctx,
            initializer=_worker_init,
            initargs=(hub_dir,),
        )
        logger.info(f"silero_pool: запущен, workers={workers}, hub={hub_dir}")
        return _POOL
    except Exception as e:
        logger.warning(f"silero_pool: не удалось создать пул: {e}")
        _POOL = None
        return None


def rebuild_pool():
    """Пересоздать пул после внезапной смерти воркера (BrokenProcessPool).

    Раньше после одной смерти child-процесса каждый submit кидал
    BrokenProcessPool до перезапуска сервера — Silero «отваливалась» навсегда.
    """
    global _POOL
    logger.warning("silero_pool: пул сломан (воркер умер), пересоздаю...")
    try:
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:
        swallow.note('silero_pool.rebuild_pool:L256')
        pass
    _POOL = None
    return init_pool(_HUB_DIR)


def is_ready():
    return _POOL is not None


def synth(text, speaker, sample_rate, timeout=30):
    """Синхронный синтез через пул. Кидает исключение при ошибке/таймауте.

    При BrokenProcessPool (воркер умер — OOM/краш) пул автоматически
    пересоздаётся и синтез повторяется один раз.
    """
    global _POOL
    if _POOL is None and _HUB_DIR:
        # Пул не поднят, но hub известен — пробуем поднять.
        init_pool(_HUB_DIR)
    if _POOL is None:
        raise RuntimeError("silero_pool не инициализирован")
    try:
        fut = _POOL.submit(_worker_synth, text, speaker, sample_rate)
        return fut.result(timeout=timeout)
    except BrokenProcessPool:
        if rebuild_pool() is None or _POOL is None:
            raise
        logger.info("silero_pool: повторная попытка синтеза после пересоздания пула")
        fut = _POOL.submit(_worker_synth, text, speaker, sample_rate)
        return fut.result(timeout=timeout)


def shutdown():
    global _POOL
    if _POOL is not None:
        try:
            _POOL.shutdown(wait=False, cancel_futures=True)
        except Exception:
            swallow.note('silero_pool.shutdown:L295')
            pass
        _POOL = None
