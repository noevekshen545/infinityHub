"""CacheManager: TTL, инвалидация и режим stale-while-revalidate."""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cache_manager import CacheManager  # noqa: E402


def _wait(predicate, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestCacheManager:
    def test_get_or_set_caches(self):
        calls = []
        cache = CacheManager(default_ttl=10.0)
        assert cache.get_or_set("k", lambda: calls.append(1) or "v") == "v"
        assert cache.get_or_set("k", lambda: calls.append(1) or "other") == "v"
        assert len(calls) == 1

    def test_invalidate_by_prefix(self):
        cache = CacheManager(default_ttl=10.0)
        cache.set("a:1", 1)
        cache.set("a:2", 2)
        cache.set("b:1", 3)
        cache.invalidate("a:")
        assert cache.get("a:1") is None
        assert cache.get("b:1") == 3


class TestStaleWhileRevalidate:
    def test_cold_call_is_synchronous(self):
        cache = CacheManager(default_ttl=0.05)
        assert cache.get_or_set_swr("k", lambda: "first", stale_ttl=5.0) == "first"

    def test_expired_entry_served_immediately_then_refreshed(self):
        cache = CacheManager(default_ttl=0.05)
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            time.sleep(0.15)
            return "v%d" % counter["n"]

        assert cache.get_or_set_swr("k", factory, stale_ttl=5.0) == "v1"
        time.sleep(0.08)                       # TTL истёк, stale ещё жив
        started = time.time()
        value = cache.get_or_set_swr("k", factory, stale_ttl=5.0)
        assert time.time() - started < 0.1     # не блокирует на 150 мс
        assert value == "v1"                   # отдано устаревшее
        assert _wait(lambda: counter["n"] == 2)
        assert _wait(lambda: cache.get("k") == "v2")

    def test_refresh_runs_once_for_concurrent_readers(self):
        cache = CacheManager(default_ttl=0.05)
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            time.sleep(0.2)
            return counter["n"]

        cache.get_or_set_swr("k", factory, stale_ttl=5.0)
        time.sleep(0.08)
        for _ in range(20):
            cache.get_or_set_swr("k", factory, stale_ttl=5.0)
        assert _wait(lambda: counter["n"] == 2)
        time.sleep(0.25)
        assert counter["n"] == 2, "параллельные читатели запустили несколько обновлений"

    def test_beyond_stale_ttl_call_is_synchronous_again(self):
        cache = CacheManager(default_ttl=0.05)
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return counter["n"]

        cache.get_or_set_swr("k", factory, stale_ttl=0.05)
        time.sleep(0.15)
        assert cache.get_or_set_swr("k", factory, stale_ttl=0.05) == 2

    def test_failed_refresh_keeps_serving_stale(self):
        cache = CacheManager(default_ttl=0.05)
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("сеть недоступна")
            return "ok"

        assert cache.get_or_set_swr("k", factory, stale_ttl=5.0) == "ok"
        time.sleep(0.08)
        assert cache.get_or_set_swr("k", factory, stale_ttl=5.0) == "ok"
        assert _wait(lambda: calls["n"] == 2)
        assert cache.get_or_set_swr("k", factory, stale_ttl=5.0) == "ok"
