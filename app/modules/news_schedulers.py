"""Планировщики новостей: однократный запуск автообновления и дайджеста.

Перенесено из server_backup.py без изменения тел. _NEWS_SCHED_STARTED пишется
через _sb: это флаг «уже запущено», который обязан быть общим, иначе каждый
вызов плодил бы второй планировщик. start_auto_refresh/start_digest_scheduler
монолит связывает ленивым импортом внутри try (строка 1036), поэтому берём их из
_sb, а не импортируем заново.
"""

import threading
import time

import server_backup as _sb

def _ensure_news_schedulers():
    """Однократно запустить планировщики новостей (потокобезопасно)."""
    global _NEWS_SCHED_STARTED
    if _sb._NEWS_SCHED_STARTED:
        return True
    with _sb._NEWS_SCHED_LOCK:
        if _sb._NEWS_SCHED_STARTED:
            return True
        _sb._NEWS_SCHED_STARTED = True
    try:
        # Автообновление по настройке (auto_refresh_minutes)
        _sb.start_auto_refresh()
        # Планировщик утреннего дайджеста (по расписанию пользователей)
        _sb.start_digest_scheduler()
    except Exception as _e:
        _sb.logger.warning(f"[news] не удалось запустить планировщики: {_e}")
    return True

def _start_news_schedulers_later(delay=15.0):
    """Фоновый поток: поднять планировщики новостей через delay секунд."""
    def _runner():
        try:
            time.sleep(delay)
            _ensure_news_schedulers()
        except Exception as _e:
            _sb.logger.debug(f"[news-sched] {_e}")
    threading.Thread(target=_runner, daemon=True, name="news-sched").start()
