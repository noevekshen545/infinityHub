"""api/workspace/attachments: ответ совпадает с прежним путём через сервер.

Раньше ручка вызывала server.workspace_get_attachments, который повторно
читал все заметки папки с диска (105 мс на 18 байт ответа), хотя те же
заметки уже лежат в кэше /api/workspace/notes. Теперь вложения собираются
из кэшированного списка; тест держит контракт: тот же набор полей, note_id
и note_title проставлены, кэш не мутирован.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routes.legacy_notes_ws as ws  # noqa: E402

_NOTES = {"articles": [
    {"id": "n1", "title": "Первая",
     "attachments": [{"name": "a.png", "url": "/api/notes/attachment?file=a.png"}]},
    {"id": "n2", "title": "Вторая", "attachments": []},
    {"id": "n3", "title": "Третья",
     "attachments": [{"name": "b.pdf", "url": "/api/notes/attachment?file=b.pdf"},
                     {"name": "c.txt", "url": "/api/notes/attachment?file=c.txt"}]},
]}


def _old_attachments(notes):
    """Прежняя реализация из modules/notes_workspace.py, дословно."""
    all_atts = []
    for n in notes.get("articles", []):
        for a in n.get("attachments", []):
            a["note_id"] = n["id"]
            a["note_title"] = n["title"]
            all_atts.append(a)
    return {"attachments": all_atts}


def test_matches_old_implementation(monkeypatch):
    import copy
    monkeypatch.setattr(ws, "api_workspace_notes",
                        lambda folder="": _async(_NOTES))
    monkeypatch.setattr(ws, "_run_sync", _run_sync)

    got = asyncio.run(ws.api_workspace_attachments(""))
    expected = _old_attachments(copy.deepcopy(_NOTES))
    assert got == expected
    assert [a["name"] for a in got["attachments"]] == ["a.png", "b.pdf", "c.txt"]


def test_cached_notes_are_not_mutated(monkeypatch):
    monkeypatch.setattr(ws, "api_workspace_notes",
                        lambda folder="": _async(_NOTES))
    monkeypatch.setattr(ws, "_run_sync", _run_sync)
    asyncio.run(ws.api_workspace_attachments(""))
    # кэш отдаётся всем читателям — дописывать note_id в его словари нельзя
    assert "note_id" not in _NOTES["articles"][0]["attachments"][0]


def _async(value):
    async def _inner():
        return value
    return _inner()


async def _run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)
