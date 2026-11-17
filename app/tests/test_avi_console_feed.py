"""Лента консоли AVI: recent_files показывает, что надзор проверял последним.

`events` фиксирует только находки/ошибки/пропуски, поэтому чистое сканирование
в консоли выглядело бы простоем. Порядок и фильтр `last_scan > 0` — контракт
панели: newest first, без ещё не проверенных строк.
"""
import os
import sqlite3
import sys

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
"""


def _db(monkeypatch, tmp_path, rows):
    data = os.path.join(str(tmp_path), "avi")
    os.makedirs(data, exist_ok=True)
    db = os.path.join(data, "avi.db")
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany(
        "insert into files(path,size,mtime,last_score,status,last_scan) "
        "values(?,1,1,?,'clean',?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(avi_module, "DB_PATH", db)
    return db


def test_нет_базы_лента_пустая(monkeypatch, tmp_path):
    monkeypatch.setattr(avi_module, "DB_PATH", os.path.join(str(tmp_path), "no.db"))
    assert avi_module.recent_files() == []


def test_сначала_самые_свежие_и_без_непроверенных(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path, [
        ("old.exe", 0.1, 100),
        ("new.exe", 0.9, 300),
        ("mid.exe", 0.5, 200),
        ("never.exe", 0.0, 0),
    ])
    rows = avi_module.recent_files(10)
    assert [r["path"] for r in rows] == ["new.exe", "mid.exe", "old.exe"]
    assert rows[0]["status"] == "clean" and rows[0]["ts"] == 300
    assert rows[0]["score"] == 0.9


def test_лимит_зажимается_и_не_ломает_сортировку(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path, [("p%s" % i, 0.0, i + 1) for i in range(5)])
    assert len(avi_module.recent_files(2)) == 2
    assert avi_module.recent_files(2)[0]["path"] == "p4"
    # 0 — «значение не передано», как у events()/findings(): берётся 50.
    assert len(avi_module.recent_files(0)) == 5
    assert len(avi_module.recent_files(10_000)) == 5


def test_индекс_последних_проверок_создаётся_один_раз(monkeypatch, tmp_path):
    """Без индекса лента сортирует всю таблицу files (310 мс на живой базе)."""
    db = _db(monkeypatch, tmp_path, [("a.exe", 0.0, 1)])
    monkeypatch.setattr(avi_module, "_index_checked", False)
    assert avi_module.ensure_indexes() is True
    con = sqlite3.connect(db)
    try:
        names = [r[1] for r in con.execute("pragma index_list(files)")]
    finally:
        con.close()
    assert "idx_files_last_scan" in names
    # Повторный вызов в том же процессе ничего не перестраивает.
    monkeypatch.setattr(avi_module, "_index_checked", False)
    assert avi_module.ensure_indexes() is False


def test_маршрут_событий_отдаёт_ленту_консоли(monkeypatch):
    """Консоль панели живёт на этом ответе: файлы, журнал и признак процесса."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.avi as R

    monkeypatch.setattr(R, "_profile", lambda request, wanted="": "main")
    monkeypatch.setattr(R, "_require_admin", lambda request: True)
    monkeypatch.setattr(avi_module, "enabled", lambda pid: True)
    monkeypatch.setattr(avi_module, "events", lambda limit=50, source="": [{"ts": 1}])
    monkeypatch.setattr(avi_module, "findings", lambda limit=50: [])
    monkeypatch.setattr(avi_module, "recent_files", lambda limit=50: [{"path": "a"}])
    monkeypatch.setattr(avi_module, "log_tail", lambda lines=40: "line")
    monkeypatch.setattr(avi_module, "current_pid", lambda: 42)

    app = FastAPI()
    app.include_router(R.router)
    with TestClient(app) as client:
        body = client.post("/api/admin/avi/events", json={}).json()
        assert body["files"] == [{"path": "a"}]
        assert body["log_tail"] == "line"
        assert body["running"] is True
        assert body["events"] == [{"ts": 1}]
