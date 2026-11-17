"""Надзор не сканирует себя: ни собственную базу, ни собственный бинарь.

Проверено на живом надзоре 02.11: в его базе оказались `data/avi/avi.db`,
`.avi.db-shm`, `.avi.db-wal` (то есть запись вердикта кормила мониторинг новыми
событиями), а копия самого `avi.exe` даёт оценку 1.0 от 13 встроенных правил —
при включённом карантине надзор перемещает собственный движок.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import avi_module  # noqa: E402


def _patched(monkeypatch, tmp_path, extra_exclude=()):
    """Модуль в изоляции: свои пути в tmp, флаги и бинарь — заглушки."""
    data = os.path.join(str(tmp_path), "avi")
    monkeypatch.setattr(avi_module, "DATA_DIR", data)
    monkeypatch.setattr(avi_module, "DB_PATH", os.path.join(data, "avi.db"))
    monkeypatch.setattr(avi_module, "MODEL_PATH", os.path.join(data, "model.json"))
    monkeypatch.setattr(avi_module, "LOG_PATH", os.path.join(data, "avi.log"))
    monkeypatch.setattr(avi_module, "PID_PATH", os.path.join(data, "avi.pid"))
    monkeypatch.setattr(avi_module, "GUARD_DIR", os.path.join(data, "guard"))
    monkeypatch.setattr(avi_module, "QUARANTINE_DIR", os.path.join(data, "quarantine"))
    exe = os.path.join(str(tmp_path), "bin", "avi.exe")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    with open(exe, "wb") as handle:
        handle.write(b"MZ")
    monkeypatch.setattr(avi_module, "binary_path", lambda: exe)
    monkeypatch.setattr(avi_module, "flags", lambda pid="": {"enabled": True})
    # Шумовые каталоги считаются от корня приложения; в тесте корень — tmp,
    # иначе результат зависел бы от того, что лежит в рабочей копии.
    monkeypatch.setattr(avi_module, "_ROOT", str(tmp_path))
    root = str(tmp_path)
    monkeypatch.setattr(
        avi_module, "settings",
        lambda pid="": dict(avi_module.DEFAULTS, roots=[root],
                            exclude=list(extra_exclude), quarantine_on=False,
                            self_guard=False, process_monitor=False))
    return data, exe, root


def _excludes(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]


def test_свои_пути_исключены(monkeypatch, tmp_path):
    data, exe, _ = _patched(monkeypatch, tmp_path)
    argv, _roots = avi_module.build_argv("")
    ex = _excludes(argv)
    for path in (data, os.path.join(data, "guard"), os.path.join(data, "quarantine"), exe):
        assert path in ex, "не исключено: " + path


def test_исключение_не_съедает_надзор_за_корнем(monkeypatch, tmp_path):
    data, exe, root = _patched(monkeypatch, tmp_path)
    argv, roots = avi_module.build_argv("")
    assert roots == [root]
    assert exe not in roots and data != root, "под исключение попал весь корень"


def test_пользовательские_исключения_остаются(monkeypatch, tmp_path):
    data, exe, root = _patched(monkeypatch, tmp_path, extra_exclude=[r"E:\scratch"])
    argv, _ = avi_module.build_argv("")
    ex = _excludes(argv)
    assert r"E:\scratch" in ex
    assert len(ex) == 4 + 1, "своих путей должно быть ровно четыре плюс user-список"


def test_шумовые_каталоги_исключаются_только_существующие(monkeypatch, tmp_path):
    """`node_modules`, `.git`, `trash` и т.п. исключаются лишь когда реально есть:
    несуществующий путь в argv — мусор, который сайдкар всё равно отбросит."""
    data, exe, root = _patched(monkeypatch, tmp_path)
    for name in ("node_modules", ".git", "trash"):
        os.makedirs(os.path.join(root, name), exist_ok=True)
    argv, _ = avi_module.build_argv("")
    ex = _excludes(argv)
    for name in ("node_modules", ".git", "trash"):
        assert os.path.join(root, name) in ex, "не исключён шум: " + name
    assert os.path.join(root, "backups") not in ex, "исключён несуществующий каталог"
    assert len(ex) == len(set(ex)) == 4 + 3, "дубли или лишние пути в --exclude"


def test_системный_монитор_передаётся_только_при_флаге(monkeypatch, tmp_path):
    """Флаг avi.sys_monitor — единственное, что поднимает службу Windows."""
    data, exe, root = _patched(monkeypatch, tmp_path)
    base = avi_module.settings("")
    monkeypatch.setattr(
        avi_module, "settings",
        lambda pid="": dict(base, sys_monitor=True, sysmon_interval=45))
    argv, _ = avi_module.build_argv("")
    assert "--with-sysmon" in argv
    assert argv[argv.index("--sysmon-interval") + 1] == "45"
    monkeypatch.setattr(
        avi_module, "settings",
        lambda pid="": dict(base, sys_monitor=False))
    argv2, _ = avi_module.build_argv("")
    assert "--with-sysmon" not in argv2 and "--sysmon-interval" not in argv2


def test_порядок_флагов_не_сдвинут(monkeypatch, tmp_path):
    """`--exclude` вставляется между `--guard` и `--no-*`: разбор аргументов в
    сайдкаре позиционный, и правка не имеет права трогать остальные флаги."""
    data, exe, root = _patched(monkeypatch, tmp_path)
    argv, _ = avi_module.build_argv("")
    assert argv[0] == exe and argv[1] == "watch"
    assert argv.index("--guard") < argv.index("--exclude")
    assert argv.index("--exclude") < argv.index("--no-self-guard")
