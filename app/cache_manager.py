"""
Унифицированное кэширование с TTL, инвалидацией по префиксу и LRU-эвакцией.
"""
import time, threading, logging
from typing import Any, Optional, Callable

logger = logging.getLogger("infinity")


class CacheManager:
    """Потокобезопасный кэш с TTL."""

    def __init__(self, default_ttl: float = 30.0, max_size: int = 1000):
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._cache: dict = {}
        self._lock = threading.RLock()
        self._refreshing: set = set()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() - entry['ts'] > entry['ttl']:
                del self._cache[key]
                return None
            entry['hits'] = entry.get('hits', 0) + 1
            return entry['val']

    def set(self, key: str, val: Any, ttl: Optional[float] = None):
        with self._lock:
            if len(self._cache) >= self._max_size:
                self._evict_lru()
            self._cache[key] = {
                'val': val,
                'ts': time.time(),
                'ttl': ttl if ttl is not None else self._default_ttl,
                'hits': 0,
            }

    def get_or_set(self, key: str, factory: Callable, ttl: Optional[float] = None) -> Any:
        existing = self.get(key)
        if existing is not None:
            return existing
        val = factory()
        self.set(key, val, ttl)
        return val

    def get_or_set_swr(self, key: str, factory: Callable, ttl: Optional[float] = None,
                       stale_ttl: Optional[float] = None) -> Any:
        """get_or_set, но устаревшее значение отдаётся сразу, а обновление идёт в фоне.

        Первый вызов (кэш пуст) считается синхронно: иначе отдавать нечего.
        После него промах TTL не блокирует запрос на время работы factory —
        это и есть смысл stale-while-revalidate. stale_ttl ограничивает,
        насколько старым может быть отдаваемое значение; по истечении
        кэш считается пустым и factory снова работает синхронно.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and stale_ttl is not None:
                if time.time() - entry['ts'] > entry['ttl'] + stale_ttl:
                    del self._cache[key]
                    entry = None
            if entry is not None and time.time() - entry['ts'] <= entry['ttl']:
                entry['hits'] = entry.get('hits', 0) + 1
                return entry['val']
            stale = entry['val'] if entry is not None else None
            already_running = key in self._refreshing

        if stale is None:
            val = factory()
            self.set(key, val, ttl)
            return val

        if not already_running:
            def _refresh():
                try:
                    self.set(key, factory(), ttl)
                except Exception:
                    logger.warning("stale-while-revalidate: обновление %r упало", key,
                                   exc_info=True)
                finally:
                    with self._lock:
                        self._refreshing.discard(key)

            with self._lock:
                self._refreshing.add(key)
            threading.Thread(target=_refresh, name=f"swr:{key}", daemon=True).start()
        return stale

    def invalidate(self, prefix: str = ""):
        with self._lock:
            if not prefix:
                self._cache.clear()
            else:
                for k in list(self._cache):
                    if k.startswith(prefix):
                        del self._cache[k]

    def invalidate_key(self, key: str):
        with self._lock:
            self._cache.pop(key, None)

    def _evict_lru(self):
        if not self._cache:
            return
        worst = min(self._cache.items(), key=lambda x: x[1].get('hits', 0))
        del self._cache[worst[0]]

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "default_ttl": self._default_ttl,
                "keys": list(self._cache.keys())[:50],
            }

    def clear(self):
        with self._lock:
            self._cache.clear()


# Глобальный экземпляр (можно импортировать в других модулях)
cache = CacheManager(default_ttl=30.0, max_size=2000)
tts_cache = CacheManager(default_ttl=300.0, max_size=500)   # TTS результаты
search_cache = CacheManager(default_ttl=60.0, max_size=1000)  # результаты поиска
