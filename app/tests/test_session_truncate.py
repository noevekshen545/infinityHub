"""Откат переписки «до выбранного сообщения» в транскрипте шлюза.

UI режет ленту локально, но контекст модели живёт в durable-транскрипте.
`SessionDB.truncate_after_message` мягко архивирует всё ПОСЛЕ целевой строки
(`active = 0`), оставляя её саму в контексте, и пересчитывает счётчики сессии.
Здесь проверяется именно это: состав активного хвоста, обратимость архива и
отказ на несуществующем id.
"""
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(APP, "hermes_module", "backend")
for path in (APP, BACKEND):
    if path not in sys.path:
        sys.path.insert(0, path)

pytest.importorskip(
    "hermes_state",
    reason="Hermes backend is not included in this repository",
)

from hermes_state import SessionDB  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session("s1", "api_server")
    yield store
    store.close()


def _seed(store):
    ids = []
    for role, text in (("user", "раз"), ("assistant", "два"),
                       ("user", "три"), ("assistant", "четыре")):
        ids.append(store.append_message("s1", role, text))
    return ids


def test_откат_оставляет_цель_и_архивирует_хвост(db):
    ids = _seed(db)

    result = db.truncate_after_message("s1", ids[1])

    assert result["truncated_count"] == 2
    assert result["new_head_id"] == ids[1]
    assert [r["content"] for r in db.get_messages("s1")] == ["раз", "два"]
    archived = db.get_messages("s1", include_inactive=True)
    assert [r["content"] for r in archived] == ["раз", "два", "три", "четыре"]
    assert [r["active"] for r in archived] == [1, 1, 0, 0]
    assert db.get_session("s1")["message_count"] == 2


def test_откат_на_последнем_сообщении_ничего_не_архивирует(db):
    ids = _seed(db)

    result = db.truncate_after_message("s1", ids[-1])

    assert result["truncated_count"] == 0
    assert result["new_head_id"] == ids[-1]
    assert len(db.get_messages("s1")) == 4


def test_второй_откат_режет_уже_сокращённый_хвост(db):
    ids = _seed(db)
    db.truncate_after_message("s1", ids[2])

    result = db.truncate_after_message("s1", ids[0])

    assert result["truncated_count"] == 2
    assert [r["content"] for r in db.get_messages("s1")] == ["раз"]
    assert db.get_session("s1")["message_count"] == 1


def test_несуществующее_сообщение_это_ошибка_а_не_молчание(db):
    _seed(db)

    with pytest.raises(ValueError):
        db.truncate_after_message("s1", 99999)


def test_сообщение_чужой_сессии_не_режет_нашу(db):
    ids = _seed(db)
    db.create_session("s2", "api_server")
    foreign = db.append_message("s2", "user", "чужое")

    with pytest.raises(ValueError):
        db.truncate_after_message("s1", foreign)

    assert len(db.get_messages("s1")) == 4
    assert [r["content"] for r in db.get_messages("s2")] == ["чужое"]
