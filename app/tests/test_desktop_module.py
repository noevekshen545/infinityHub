"""desktop_module: кэш списка установленных пакетов и источник Appx-имён.

Get-AppxPackage (PowerShell) стоит ~0.9 с на этой машине и вызывался на
каждый запрос списка приложений. Эти тесты фиксируют оба лекарства:
реестровый источник того же списка и stale-while-revalidate вокруг кэша.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop_module as dm  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_cache():
    saved_value = dm._installed_cache["value"]
    saved_ts = dm._installed_cache["ts"]
    yield
    dm._installed_cache["value"] = saved_value
    dm._installed_cache["ts"] = saved_ts


class TestAppxNames:
    def test_registry_returns_lowercase_short_names(self):
        names = dm._appx_names_registry()
        if names is None:
            pytest.skip("реестр пакетов недоступен на этой системе")
        assert names, "реестр пуст"
        for name in names:
            assert name == name.lower()
            assert "_" not in name, name

    def test_registry_covers_powershell_names(self):
        """PowerShell — эталон; реестр обязан видеть те же пакеты (или больше).

        Единственное известное расхождение — windows.immersivecontrolpanel:
        он поставляется в SystemApps и в HKCU-ветке Repository не значится.
        """
        if os.name != "nt":
            pytest.skip("тест Windows-специфичен")
        registry = dm._appx_names_registry()
        if registry is None:
            pytest.skip("реестр пакетов недоступен")
        powershell = dm._appx_names_powershell()
        if not powershell:
            pytest.skip("PowerShell недоступен")
        missing = powershell - registry
        assert missing <= {"windows.immersivecontrolpanel"}, missing
        assert not (registry - powershell)


class TestInstalledCache:
    def test_cold_scan_is_cached(self, monkeypatch):
        scans = []
        monkeypatch.setattr(dm, "_scan_installed_packages",
                            lambda: scans.append(1) or {"vscode"})
        dm._installed_cache["value"] = None
        assert dm._get_installed_packages() == {"vscode"}
        assert dm._get_installed_packages() == {"vscode"}
        assert len(scans) == 1

    def test_expired_cache_serves_stale_immediately_and_refreshes(self, monkeypatch):
        started = threading.Event()

        def slow_scan():
            started.set()
            time.sleep(0.3)
            return {"fresh"}

        monkeypatch.setattr(dm, "_scan_installed_packages", slow_scan)
        dm._installed_cache["value"] = {"old"}
        dm._installed_cache["ts"] = 0            # TTL истёк

        began = time.time()
        assert dm._get_installed_packages() == {"old"}
        assert time.time() - began < 0.2, "запрос ждал скан вместо stale-ответа"
        assert started.wait(2.0)

        deadline = time.time() + 2.0
        while time.time() < deadline and dm._installed_cache["value"] != {"fresh"}:
            time.sleep(0.01)
        assert dm._installed_cache["value"] == {"fresh"}

    def test_concurrent_readers_trigger_one_refresh(self, monkeypatch):
        calls = []

        def slow_scan():
            calls.append(1)
            time.sleep(0.2)
            return {"fresh"}

        monkeypatch.setattr(dm, "_scan_installed_packages", slow_scan)
        dm._installed_cache["value"] = {"old"}
        dm._installed_cache["ts"] = 0
        threads = [threading.Thread(target=dm._get_installed_packages) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.4)
        assert calls == [1]

    def test_force_rescans_synchronously(self, monkeypatch):
        monkeypatch.setattr(dm, "_scan_installed_packages", lambda: {"forced"})
        dm._installed_cache["value"] = {"old"}
        dm._installed_cache["ts"] = time.time()
        assert dm._get_installed_packages(force=True) == {"forced"}

    def test_top100_uses_cached_scan(self, monkeypatch):
        monkeypatch.setattr(dm, "_scan_installed_packages", lambda: {"vscode"})
        dm._installed_cache["value"] = None
        began = time.time()
        apps = dm.get_top100_apps()
        assert time.time() - began < 0.5
        assert len(apps) == 100
        assert dm._installed_cache["value"] == {"vscode"}
