"""Поиск VLC в PATH без подпроцесса и с коротким кэшем.

Прежний find_vlc запускал `where vlc` — 176 мс на машине без установленного
VLC, и это единственная статья холодного /api/media/vlc-check, вылезавшая за
200 мс. shutil.which даёт тот же ответ внутри процесса. Кэш обязан не
залипать навсегда: установленный заново VLC должен находиться сам.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vlc_streamer as v  # noqa: E402


class _Counter:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def __call__(self, name):
        self.calls += 1
        return self.answer


def test_which_vlc_ne_zapuskaet_podprocess(monkeypatch):
    import shutil
    hit = _Counter(r"C:\vlc\vlc.exe")
    monkeypatch.setattr(shutil, "which", hit)
    monkeypatch.setattr(v, "_WHICH_CACHE", {"path": None, "ts": 0.0})
    assert v._which_vlc() == r"C:\vlc\vlc.exe"
    assert hit.calls == 1


def test_povtornyy_vyzov_beretsya_iz_kesha(monkeypatch):
    import shutil
    hit = _Counter(None)
    monkeypatch.setattr(shutil, "which", hit)
    monkeypatch.setattr(v, "_WHICH_CACHE", {"path": None, "ts": 0.0})
    v._which_vlc()
    v._which_vlc()
    v._which_vlc()
    assert hit.calls == 1


def test_kesh_istekaet_i_vlc_nahoditsya_znovo(monkeypatch):
    import shutil
    hit = _Counter(None)
    monkeypatch.setattr(shutil, "which", hit)
    # запись просрочена на час — следующая проверка обязана спросить PATH
    monkeypatch.setattr(v, "_WHICH_CACHE", {"path": None, "ts": 1.0})
    assert v._which_vlc() is None
    hit.answer = r"C:\vlc\vlc.exe"
    # сдвигаем метку в прошлое, не дожидаясь реального TTL
    v._WHICH_CACHE["ts"] = 1.0
    assert v._which_vlc() == r"C:\vlc\vlc.exe"
    assert hit.calls == 2


def test_oshibka_which_ne_ronyaet_find_vlc(monkeypatch):
    import shutil

    def boom(name):
        raise OSError("PATH недоступен")

    monkeypatch.setattr(shutil, "which", boom)
    monkeypatch.setattr(v, "_WHICH_CACHE", {"path": None, "ts": 0.0})
    assert v._which_vlc() is None
    # find_vlc живёт и без PATH: в этой сборке portable-VLC лежит в комплекте
    monkeypatch.setattr(v, "_extracted_vlc", lambda: None)
    monkeypatch.setattr(v, "_win_bundle_dirs", lambda: [])
    assert v.find_vlc() is None
