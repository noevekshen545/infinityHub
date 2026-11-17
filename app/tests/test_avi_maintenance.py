"""Ретенция журнала надзора: удаляются только старые события.

Замер живой базы 03.11: `events` растёт десятками тысяч строк в сутки (шум
`app/data/*-wal`, `node_modules`), а `files` и `verdicts` — рабочие данные и
обучающая выборка. Обслуживание не имеет права трогать ни то, ни другое.
"""
import os
import sqlite3
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import avi_module  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, size INTEGER NOT NULL,
    mtime INTEGER NOT NULL, sha256 TEXT, last_score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown', fp INTEGER NOT NULL DEFAULT 0,
    last_scan INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, path TEXT NOT NULL,
    sha256 TEXT, source TEXT NOT NULL, detail TEXT NOT NULL,
    score REAL NOT NULL, action TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, original_path TEXT NOT NULL,
    quarantine_path TEXT NOT NULL, sha256 TEXT NOT NULL,
    reason TEXT NOT NULL, score REAL NOT NULL);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, sha256 TEXT NOT NULL,
    path TEXT NOT NULL, label TEXT NOT NULL, reason TEXT,
    features_json TEXT);
"""


def _db(monkeypatch, tmp_path, events=(), days_retention=3):
    data = os.path.join(str(tmp_path), "avi")
    os.makedirs(data, exist_ok=True)
    db = os.path.join(data, "avi.db")
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute("insert into files(path,size,mtime,last_scan) values('a',1,1,1)")
    con.execute("insert into verdicts(ts,sha256,path,label) values(1,'h','a','clean')")
    con.executemany(
        "insert into events(ts,path,source,detail,score,action) values(?,?,?,?,?,?)",
        events)
    con.commit()
    con.close()
    monkeypatch.setattr(avi_module, "DATA_DIR", data)
    monkeypatch.setattr(avi_module, "DB_PATH", db)
    monkeypatch.setattr(
        avi_module, "settings",
        lambda pid="": dict(avi_module.DEFAULTS, retention_days=days_retention))
    return db


def _event(ts, path="x"):
    return (ts, path, "watcher", "", 0.0, "seen")


def _count(db, table):
    con = sqlite3.connect(db)
    try:
        return con.execute("select count(*) from " + table).fetchone()[0]
    finally:
        con.close()


def test_нет_базы_обслуживание_отказывает(monkeypatch, tmp_path):
    monkeypatch.setattr(avi_module, "DB_PATH", os.path.join(str(tmp_path), "nope.db"))
    assert avi_module.db_info()["exists"] is False
    assert avi_module.maintain(days=3)["ok"] is False


def test_старые_события_удаляются_рабочие_данные_целы(monkeypatch, tmp_path):
    now = int(time.time())
    db = _db(monkeypatch, tmp_path, [
        _event(now - 10 * 86400, "old"),
        _event(now - 4 * 86400, "old2"),
        _event(now - 3600, "fresh"),
    ])
    res = avi_module.maintain(days=3)
    assert res["ok"] is True and res["removed_events"] == 2
    assert _count(db, "events") == 1
    assert _count(db, "files") == 1, "рабочие файлы не должны удаляться"
    assert _count(db, "verdicts") == 1, "обучающая выборка не должна удаляться"
    assert res["after"]["events"] == 1
    con = sqlite3.connect(db)
    try:
        idx = [r[1] for r in con.execute("pragma index_list(events)")]
    finally:
        con.close()
    assert "idx_events_ts" in idx


def test_повторный_прогон_ничего_не_находит(monkeypatch, tmp_path):
    now = int(time.time())
    _db(monkeypatch, tmp_path, [_event(now - 10 * 86400), _event(now)])
    first = avi_module.maintain(days=3)
    second = avi_module.maintain(days=3)
    assert first["removed_events"] == 1
    assert second["removed_events"] == 0
    assert second["batches"] == 1


def test_удаление_идёт_батчами(monkeypatch, tmp_path):
    """Больше лимита батча (5000) — значит два прохода, а не одна гигантская
    транзакция, которая держала бы блокировку у живого надзора."""
    now = int(time.time())
    old = now - 10 * 86400
    db = _db(monkeypatch, tmp_path,
             [_event(old, "p%s" % i) for i in range(5001)] + [_event(now)])
    res = avi_module.maintain(days=3, vacuum=False)
    assert res["removed_events"] == 5001
    assert res["batches"] >= 2
    assert _count(db, "events") == 1


def test_дни_берутся_из_настроек(monkeypatch, tmp_path):
    now = int(time.time())
    _db(monkeypatch, tmp_path, [_event(now - 5 * 86400)], days_retention=3)
    res = avi_module.maintain(vacuum=False)
    assert res["days"] == 3
    assert res["removed_events"] == 1


def test_нулевая_ретенция_зажимается_в_минимум(monkeypatch, tmp_path):
    """`days=0` означал бы «удалить всё, включая сегодняшнее» — зажимаем в 1."""
    now = int(time.time())
    _db(monkeypatch, tmp_path, [_event(now - 2 * 86400), _event(now)])
    res = avi_module.maintain(days=0, vacuum=False)
    assert res["days"] == 1
    assert res["removed_events"] == 1
