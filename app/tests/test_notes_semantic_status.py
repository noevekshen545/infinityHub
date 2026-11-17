"""notes_semantic_module.status: мемо по сигнатуре и инвалидация правкой.

Ручка /api/notes/semantic/status отдаёт только счётчики, но чтобы их
посчитать, модуль перечитывает все заметки (на живой базе 53 файла с
base64-картинками — 145–195 мс). Мемо привязано к сигнатуре каталога и
отметке файла кэша, а не к таймеру: сохранение заметки обязано сразу
дать новый ответ, иначе админка покажет устаревший fresh.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notes_semantic_module as sem  # noqa: E402


def _note(path, nid, updated, content="текст"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"id": nid, "title": nid, "content": content,
                   "updated": updated}, f, ensure_ascii=False)


def _reset_memo(monkeypatch, cache_dir):
    monkeypatch.setattr(sem, "_status_memo", None)
    monkeypatch.setattr(sem, "CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(sem, "feature_enabled", lambda: False)


def test_repeat_call_is_memoized(tmp_path, monkeypatch):
    root = str(tmp_path / "notes")
    _reset_memo(monkeypatch, tmp_path / "cache")
    _note(os.path.join(root, "a.json"), "a", 1)

    calls = []
    real = sem._status_compute

    def counted(notes_dir):
        calls.append(notes_dir)
        return real(notes_dir)

    monkeypatch.setattr(sem, "_status_compute", counted)
    sem.status(root)
    sem.status(root)
    assert len(calls) == 1


def test_edit_invalidates(tmp_path, monkeypatch):
    root = str(tmp_path / "notes")
    _reset_memo(monkeypatch, tmp_path / "cache")
    path = os.path.join(root, "a.json")
    _note(path, "a", 1)
    assert sem.status(root)["notes"] == 1

    time.sleep(0.01)
    _note(path, "a", 2, content="")
    second = sem.status(root)
    assert second["notes"] == 1
    # updated сменился -> сигнатура корпуса другая; кэша групп нет, значит
    # ready=False и stale=False (нечего помечать устаревшим)
    assert second["ready"] is False and second["stale"] is False


def test_cache_file_refreshes_status(tmp_path, monkeypatch):
    root = str(tmp_path / "notes")
    _reset_memo(monkeypatch, tmp_path / "cache")
    _note(os.path.join(root, "a.json"), "a", 1)
    assert sem.status(root)["ready"] is False

    notes = sem._active_notes(root)
    sem._write_cache(root, {"version": sem.CACHE_VERSION,
                            "signature": sem.signature(notes),
                            "generated": 42, "engine": "test",
                            "groups": [{"key": "work"}], "assignments": {}})
    after = sem.status(root)
    assert after["ready"] is True and after["fresh"] is True
    assert after["generated"] == 42 and after["engine"] == "test"


def test_feature_flag_is_read_every_call(tmp_path, monkeypatch):
    root = str(tmp_path / "notes")
    _reset_memo(monkeypatch, tmp_path / "cache")
    _note(os.path.join(root, "a.json"), "a", 1)
    assert sem.status(root)["feature"] is False
    monkeypatch.setattr(sem, "feature_enabled", lambda: True)
    # флаг переключается в настройках — мемо не должно его залиплять
    assert sem.status(root)["feature"] is True
