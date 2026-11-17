"""
Wake Word Engine — серверная часть детекции слова пробуждения.

Клиент шлёт 16 kHz int16 PCM короткими чанками на /api/wakeword/listen.
Сервер держит скользящее окно ~3 с и прогоняет его через лёгкую модель
faster-whisper (base, жадное декодирование), но только когда в аудио есть речь.

Почему так: у openWakeWord нет русских моделей из коробки, поэтому русское
слово распознаётся через STT. Раньше на каждый чанк транскрибировался весь
8-секундный буфер моделью small — это секунды на CPU, запросы копились, и
слово срабатывало с большой задержкой. Теперь:

  * окно короткое (3 с), распознаётся только при наличии речи (RMS-гейт
    относительно адаптивного шума);
  * распознавание — отдельной лёгкой моделью base с beam_size=1
    (в разы быстрее small и не отбирает потоки у обычного STT);
  * одновременно идёт не больше одной транскрипции — остальные чанки
    просто пропускаются;
  * совпадение нечёткое: «финик/финики/феник/финек» и т.п.;
  * после срабатывания — пауза и очистка буфера, чтобы не сработать повторно
    на собственный TTS или на ту же фразу.

Английские слова по-прежнему идут через openWakeWord, если он установлен.
"""
import os, sys, json, re, math, time, array, wave, tempfile, logging, threading
from collections import deque
import swallow

logger = logging.getLogger("infinity")

_BASE = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_BASE, "data", "wakeword_state.json")

# Слова, которые регистрируются, если в state ещё вообще ничего нет.
# «финик» — текущее слово пробуждения, «аватар» — прежнее, чтобы старое
# поведение не потерялось.
_DEFAULT_WORDS = ("финик", "аватар")

_SR = 16000
_WINDOW_S = 3.0          # сколько аудио держим в буфере
_MIN_S = 0.9             # минимум аудио перед первой транскрипцией
_SPEECH_HOLD_S = 1.5     # сколько ещё пробуем после последней речи
_MIN_INTERVAL_S = 0.6    # пауза между транскрипциями
_COOLDOWN_S = 3.0        # пауза после срабатывания

_LOCK = threading.Lock()
_STT_BUSY = threading.Lock()          # не больше одной транскрипции одновременно
_BUF = bytearray()                    # сырые int16 LE, только последние _WINDOW_S
_RMS_HIST = deque(maxlen=20)          # RMS последних чанков → оценка шума
_ST = {"last_speech": 0.0, "last_run": 0.0, "last_detect": 0.0,
       "warming": False, "warm_tries": 0}

_USER_WORDS = {}  # совместимость со старым API (не используется)
_OWW_MODEL = None
_OWW_AVAILABLE = None
_OWW_FAILS = 0


# ---------------------------------------------------------------- состояние

def _ensure_state():
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    if not os.path.exists(_STATE_FILE):
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"words": {}}, f, ensure_ascii=False, indent=2)


def _load_state():
    _ensure_state()
    with open(_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    words = state.setdefault("words", {})
    if not words:
        now = time.time()
        for w in _DEFAULT_WORDS:
            words[w] = {"threshold": 0.5, "enabled": True, "added": now}
        _save_state(state)
    return state


def _save_state(state):
    _ensure_state()
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- openWakeWord

def _is_oww_available() -> bool:
    global _OWW_AVAILABLE
    if _OWW_AVAILABLE is not None:
        return _OWW_AVAILABLE
    try:
        import openwakeword  # noqa
        from openwakeword.model import Model  # noqa
        _OWW_AVAILABLE = True
    except Exception as e:
        _OWW_AVAILABLE = False
        logger.info(f"[WAKEWORD] openwakeword не доступен: {e}")
    return _OWW_AVAILABLE


def _get_oww_model():
    """Загрузить openWakeWord модель (предобученные 'alexa', 'hey_jarvis')."""
    global _OWW_MODEL
    if _OWW_MODEL is not None:
        return _OWW_MODEL
    if not _is_oww_available():
        return None
    try:
        from openwakeword.model import Model
        _OWW_MODEL = Model(
            wakeword_models=["alexa", "hey_jarvis"],
            inference_framework="onnx"
        )
        return _OWW_MODEL
    except Exception as e:
        logger.error(f"[WAKEWORD] OWW load error: {e}")
        return None


def _detect_oww(pcm: bytes):
    """Английские модели openWakeWord. None — если проверять нечем."""
    global _OWW_FAILS
    if _OWW_FAILS >= 3:
        return None
    oww = _get_oww_model()
    if oww is None:
        return None
    try:
        import numpy as np
        samples = np.frombuffer(pcm, dtype=np.int16)
        oww.predict(samples)
        for name, score_arr in oww.prediction_buffer.items():
            if not len(score_arr):
                continue
            score = float(score_arr[-1])
            if score > 0.5:
                w = "джарвис" if "jarvis" in name else ("алекса" if "alexa" in name else name)
                return {"detected": True, "word": w, "score": score, "source": "oww"}
    except Exception as e:
        _OWW_FAILS += 1
        logger.warning(f"[WAKEWORD] oww predict failed: {e}")
    return None


# ---------------------------------------------------------------- речь/шум

def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n <= 0:
        return 0.0
    try:
        import numpy as np
        a = np.frombuffer(pcm[:n * 2], dtype="<i2").astype(np.float32)
        return float(np.sqrt(np.mean(a * a)))
    except Exception:
        swallow.note('wakeword_engine._rms:L157')
        a = array.array("h")
        a.frombytes(pcm[:n * 2])
        if sys.byteorder == "big":
            a.byteswap()
        s = 0
        for v in a:
            s += v * v
        return math.sqrt(s / n)


def _is_speech(rms: float) -> bool:
    """RMS выше фона: порог = max(110, 2.2 × минимум за последние ~10 с).

    Минимум по истории вместо EMA: стационарный шум (вентилятор, кондиционер)
    поднимает порог, а короткое слово на его фоне всё равно проходит.
    """
    _RMS_HIST.append(rms)
    floor = min(_RMS_HIST) if _RMS_HIST else 0.0
    threshold = max(110.0, min(2000.0, floor * 2.2))
    return rms >= threshold


# ---------------------------------------------------------------- совпадение

_TOKEN_RE = re.compile(r"[0-9a-zа-я]+")


def _norm_text(s: str) -> str:
    return " ".join(_TOKEN_RE.findall((s or "").lower().replace("ё", "е")))


def _dist(a: str, b: str, maxd: int = 2) -> int:
    """Расстояние Левенштейна с ранним выходом (строки короткие)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > maxd:
        return maxd + 1
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        rowmin = i
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            v = prev[j] + 1
            if cur[j - 1] + 1 < v:
                v = cur[j - 1] + 1
            if prev[j - 1] + cost < v:
                v = prev[j - 1] + cost
            cur[j] = v
            if v < rowmin:
                rowmin = v
        if rowmin > maxd:
            return maxd + 1
        prev = cur
    return prev[lb]


def _match_word(word: str, text: str):
    """Насколько текст похож на слово: (score 0..1, что именно совпало).

    Точное совпадение — 1.0, слово с окончанием («финики») — 0.92,
    одна опечатка распознавания («феник», «финек») — 0.85.
    """
    w = _norm_text(word)
    if not w:
        return 0.0, ""
    if " " in w:
        return (1.0 if w in text else 0.0), w
    tokens = text.split()
    best, hit = 0.0, ""
    for t in tokens:
        if t == w:
            return 1.0, t
        if t.startswith(w) and 0 < len(t) - len(w) <= 2:
            if 0.92 > best:
                best, hit = 0.92, t
        elif len(w) >= 4 and _dist(t, w, 1) <= 1:
            if 0.85 > best:
                best, hit = 0.85, t
    # whisper иногда делит слово на два: «фи ник»
    for i in range(len(tokens) - 1):
        joined = tokens[i] + tokens[i + 1]
        if joined == w or (len(w) >= 4 and _dist(joined, w, 1) <= 1):
            if 0.8 > best:
                best, hit = 0.8, joined
            break
    return best, hit


# ---------------------------------------------------------------- модели

def _warm_short_model_async():
    """Заранее поднять лёгкую модель, пока пользователь ещё говорит."""
    if _ST["warming"] or _ST["warm_tries"] >= 3:
        return
    try:
        import stt_engine
    except Exception:
        swallow.note('wakeword_engine._warm_short_model_async:L256')
        return
    _ST["warming"] = True

    def _run():
        ok = False
        try:
            ok = stt_engine.ensure_short_model()
        except Exception:
            swallow.note('wakeword_engine._run:L265')
            ok = False
        finally:
            _ST["warming"] = False
            _ST["warm_tries"] = 3 if ok else _ST["warm_tries"] + 1

    threading.Thread(target=_run, daemon=True, name="wakeword-model-warm").start()


def _transcribe_window(pcm: bytes, sample_rate: int) -> str:
    """PCM int16 → WAV → быстрый STT. Возвращает нормализованный текст."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        import stt_engine
        result = stt_engine.transcribe_short(tmp, language="ru")
        return _norm_text(result.get("text") or "")
    except Exception as e:
        logger.warning(f"[WAKEWORD] stt window error: {e}")
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                swallow.note('wakeword_engine._transcribe_window:L296')
                pass


# ---------------------------------------------------------------- публичный API

def register_word(word: str, threshold: float = 0.5):
    """Зарегистрировать пользовательское слово (русское — через STT)."""
    with _LOCK:
        state = _load_state()
        w = _normalize_word(word)
        if not w:
            return False
        state.setdefault("words", {})[w] = {
            "threshold": threshold,
            "enabled": True,
            "added": time.time(),
        }
        _save_state(state)
    # Прогреваем модели в фоне: загрузка занимает секунды и не должна
    # блокировать HTTP-ответ.
    try:
        import stt_engine
        if stt_engine.is_available():
            def _warm():
                try:
                    stt_engine.ensure_loaded()
                except Exception:
                    swallow.note('wakeword_engine._warm:L324')
                    pass
                try:
                    stt_engine.ensure_short_model()
                except Exception:
                    swallow.note('wakeword_engine._warm:L329')
                    pass
            threading.Thread(target=_warm, daemon=True, name="wakeword-stt-warmup").start()
    except Exception:
        swallow.note('wakeword_engine.register_word:L333')
        pass
    logger.info(f"[WAKEWORD] registered word='{w}' threshold={threshold}")
    return True


def _normalize_word(word: str) -> str:
    return _norm_text(word)


def list_words() -> list:
    return list(_load_state().get("words", {}).keys())


def detect_pcm(pcm_int16_bytes: bytes, sample_rate: int = 16000) -> dict:
    """Принять PCM int16 mono и вернуть {detected, word, score, source}."""
    sr = int(sample_rate or _SR)
    if not pcm_int16_bytes:
        return {"detected": False}
    pcm = bytes(pcm_int16_bytes[: (len(pcm_int16_bytes) // 2) * 2])

    # 1) Английские модели openWakeWord (если установлен)
    got = _detect_oww(pcm)
    if got:
        return got

    # 2) Русские слова: буфер + гейт по речи + быстрый STT
    _warm_short_model_async()
    rms = _rms(pcm)
    now = time.time()
    with _LOCK:
        _BUF.extend(pcm)
        max_bytes = int(sr * _WINDOW_S) * 2
        if len(_BUF) > max_bytes:
            del _BUF[: len(_BUF) - max_bytes]
        if _is_speech(rms):
            _ST["last_speech"] = now
        buf_len = len(_BUF)

    if now - _ST["last_detect"] < _COOLDOWN_S:
        return {"detected": False}
    if buf_len < int(sr * _MIN_S) * 2:
        return {"detected": False}
    if now - _ST["last_speech"] > _SPEECH_HOLD_S:
        return {"detected": False}
    if now - _ST["last_run"] < _MIN_INTERVAL_S:
        return {"detected": False}
    if not _STT_BUSY.acquire(blocking=False):
        # Предыдущая транскрипция ещё считается — не копим очередь.
        return {"detected": False, "busy": True}

    try:
        _ST["last_run"] = time.time()
        with _LOCK:
            data = bytes(_BUF)
        text = _transcribe_window(data, sr)
        if not text:
            return {"detected": False}

        words = _load_state().get("words", {})
        best_score, best_word, best_hit = 0.0, "", ""
        for w, meta in words.items():
            if not meta.get("enabled", True):
                continue
            score, hit = _match_word(w, text)
            if score > best_score:
                best_score, best_word, best_hit = score, w, hit

        if best_word:
            threshold = float(words[best_word].get("threshold", 0.5))
            if best_score >= max(0.6, threshold):
                with _LOCK:
                    _ST["last_detect"] = time.time()
                    _BUF.clear()
                logger.info(f"[WAKEWORD] detected '{best_word}' "
                            f"(score={best_score:.2f}, hit='{best_hit}', text='{text[:60]}')")
                return {"detected": True, "word": best_word, "score": best_score,
                        "source": "stt", "transcript": text}
        return {"detected": False, "transcript": text}
    finally:
        _STT_BUSY.release()


def status() -> dict:
    model = None
    try:
        import stt_engine
        model = getattr(stt_engine, "_SHORT_NAME", None)
    except Exception:
        swallow.note('wakeword_engine.status:L422')
        pass
    return {
        "oww_available": _is_oww_available(),
        "words": list_words(),
        "buffer_bytes": len(_BUF),
        "model": model,
    }
