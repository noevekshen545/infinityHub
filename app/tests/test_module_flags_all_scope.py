"""«Все профили» в админке: сводка — пересечение, запись — в каждый профиль.

Режим добавлен для флагов модулей и вкладки AVI. Ошибка здесь тихая: панель
показывает «включено», а у части профилей флаг выключен, или запись уходит
только активному. Оба случая проверяются на подменённом списке профилей.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import module_flags  # noqa: E402


def test_сводка_считает_флаг_включённым_только_у_всех(monkeypatch):
    snap = {
        "a": {"avi": {"enabled": True, "sys_monitor": False}},
        "b": {"avi": {"enabled": True, "sys_monitor": True}},
    }
    monkeypatch.setattr(module_flags, "all_profile_ids", lambda: ["a", "b"])
    monkeypatch.setattr(module_flags, "all_flags_for", lambda pid="": snap[pid])
    out = module_flags.flags_every()
    assert out["flags"]["avi"]["enabled"] is True
    assert out["flags"]["avi"]["sys_monitor"] is False
    assert "avi.sys_monitor" in out["partial"]
    assert "avi.enabled" not in out["partial"]


def test_запись_всем_идёт_в_каждый_профиль(monkeypatch):
    seen = []
    monkeypatch.setattr(module_flags, "all_profile_ids", lambda: ["a", "b", "c"])
    monkeypatch.setattr(
        module_flags, "set_flag_for",
        lambda module, key, value, pid="": seen.append((module, key, value, pid)))
    saved = module_flags.set_flag_every("avi", "sys_monitor", True)
    assert [row[3] for row in seen] == ["a", "b", "c"]
    assert all(row[0] == "avi" and row[1] == "sys_monitor" and row[2] is True
               for row in seen)
    assert saved["profiles"] == ["a", "b", "c"]


def test_без_профилей_запись_отказывает(monkeypatch):
    monkeypatch.setattr(module_flags, "all_profile_ids", lambda: [])
    try:
        module_flags.set_flag_every("avi", "sys_monitor", True)
    except RuntimeError as exc:
        assert "Профили не найдены" in str(exc)
    else:
        raise AssertionError("запись без профилей должна падать, а не молчать")
