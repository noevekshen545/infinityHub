"""Мелкие фоновые запускалки: отложенный прогрев TTS и псевдоним статуса
LLM-сервера.

Перенесено из server_backup.py без изменения тела. Событие готовности
(_TTS_READY_EVENT) и задержка (_TTS_WARMUP_DELAY) читаются через _sb: событие
взводит другой код монолита, и своя копия Event никогда бы не проснулась.
_check_lm_studio_status в монолите - не функция, а присваивание-псевдоним на
_check_lm_studio_health (строка 3529), поэтому здесь это тонкая обёртка над уже
нативным modules.llm_text: тело копируется один раз, а имя остаётся за этим
модулем (проверка происхождения иначе считала бы его непересаженным).
"""

import threading
import time

import server_backup as _sb
from modules.llm_text import _check_lm_studio_health


def _check_lm_studio_status():
    return _check_lm_studio_health()

def _warmup_tts_later(delay=None):
    """Через delay секунд поднять TTS в фоне (не блокирует старт)."""
    delay = _sb._TTS_WARMUP_DELAY if delay is None else delay
    def _runner():
        try:
            if _sb._TTS_READY_EVENT.wait(0):
                return
            time.sleep(delay)
            _sb._start_tts_init_async()
        except Exception as _e:
            _sb.logger.debug(f"[tts-warmup] {_e}")
    threading.Thread(target=_runner, daemon=True, name="tts-warmup").start()
