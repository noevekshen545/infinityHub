"""Режим ботов: комнаты, круги, авто-режим, изоляция профилей.

Сеть не нужна: ход бота подменяется фейковой функцией, поэтому тесты
проверяют именно поведение круга — порядок реплик, ошибку одного бота,
остановку и попадание реплики пользователя в контекст следующего бота.
"""
import asyncio
import json
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import agentai_bots as bots  # noqa: E402

PERSONAS = [
    {"name": "critic", "display_name": "Критик", "icon": "fa-bug",
     "description": "риски", "model": ""},
    {"name": "architect", "display_name": "Архитектор", "icon": "fa-cubes",
     "description": "структура", "model": ""},
    {"name": "tester", "display_name": "Тестировщик", "icon": "fa-vial",
     "description": "проверки", "model": ""},
]


@pytest.fixture()
def rooms(tmp_path, monkeypatch):
    root = tmp_path / "rooms"
    root.mkdir()
    monkeypatch.setattr(bots, "rooms_dir", lambda pid: str(root))
    monkeypatch.setattr(bots, "personas_catalog", lambda: [dict(p) for p in PERSONAS])
    bots._RUNNING.clear()
    bots._STOP.clear()
    return root


def _team(names=("critic", "architect")):
    return [{"persona": n, "role": n + "-роль"} for n in names]


def _room(pid="default", names=("critic", "architect"), topic="Свести план"):
    return bots.create_room(pid, "План", topic, _team(names))


def _fake_ask(calls, fail=()):
    async def ask(room, bot, message):
        calls.append({"persona": bot["persona"], "message": message})
        if bot["persona"] in fail:
            raise bots.BotsError("шлюз недоступен", 502, "gateway_error")
        return "ответ %s" % bot["persona"], "sid-%s" % bot["persona"]
    return ask


def test_комната_сохраняется_и_видна_списку(rooms):
    room = _room()
    assert bots.ROOM_ID_RE.match(room["id"])
    loaded = bots.load_room("default", room["id"])
    assert loaded["title"] == "План"
    assert [b["display_name"] for b in loaded["bots"]] == ["Критик", "Архитектор"]
    rows = bots.list_rooms("default")
    assert len(rows) == 1 and rows[0]["id"] == room["id"]


def test_минимум_два_бота(rooms):
    with pytest.raises(bots.BotsError) as err:
        bots.create_room("default", "x", "x", _team(("critic",)))
    assert err.value.code == "too_few_bots"


def test_дубль_личности_не_считается_двумя(rooms):
    with pytest.raises(bots.BotsError):
        bots.create_room("default", "x", "x",
                         _team(("critic", "critic")))


def test_неизвестная_личность_отвергается(rooms):
    with pytest.raises(bots.BotsError) as err:
        bots.create_room("default", "x", "x", _team(("critic", "nobody")))
    assert err.value.code == "persona_not_found"


def test_круг_даёт_по_реплике_от_каждого(rooms, monkeypatch):
    room = _room()
    calls = []
    monkeypatch.setattr(bots, "ask_persona", _fake_ask(calls))
    asyncio.run(bots._worker("default", room["id"], 1, 0.0))
    got = bots.load_room("default", room["id"])
    assert got["status"] == "idle"
    assert len(got["rounds"]) == 1 and got["rounds"][0]["ok"] == 2
    replies = [m for m in got["messages"] if m["kind"] == "bot"]
    assert [m["bot"] for m in replies] == ["critic", "architect"]
    assert replies[0]["round"] == 1 and replies[1]["round"] == 1
    # Сессии сохраняются: следующий круг продолжит те же переписки.
    assert got["sessions"]["critic"]["sid"] == "sid-critic"
    # Второй бот видит реплику первого внутри круга.
    assert "ответ critic" in calls[1]["message"]


def test_ошибка_одного_бота_не_ломает_круг(rooms, monkeypatch):
    room = _room()
    calls = []
    monkeypatch.setattr(bots, "ask_persona", _fake_ask(calls, fail=("critic",)))
    asyncio.run(bots._worker("default", room["id"], 1, 0.0))
    got = bots.load_room("default", room["id"])
    assert got["rounds"][0]["ok"] == 1 and got["rounds"][0]["failed"] == 1
    kinds = [(m["kind"], m.get("bot"), bool(m.get("error")))
             for m in got["messages"]]
    assert ("bot", "critic", True) in kinds
    assert ("bot", "architect", False) in kinds
    assert "шлюз недоступен" in got["last_error"]


def test_пустой_текст_исключения_не_оставляет_ошибку_без_причины(rooms, monkeypatch):
    """Сетевые исключения бывают с пустым str(): в ленте оставалось «Ошибка: »."""
    class Bare(Exception):
        def __str__(self):
            return ""

    room = _room()

    async def ask(room_obj, bot, message):
        raise Bare()
    monkeypatch.setattr(bots, "ask_persona", ask)
    asyncio.run(bots._worker("default", room["id"], 1, 0.0))
    got = bots.load_room("default", room["id"])
    texts = [m["text"] for m in got["messages"] if m.get("error")]
    assert texts and all(t.strip() != "Ошибка:" for t in texts)
    assert "Bare" in got["last_error"]


def test_занятый_заголовок_возвращает_id_существующей_сессии(rooms):
    text = ("Title 'bots · Обсуждение ботов · default [botroom-76dade4da2]' "
            "is already in use by session api_1793981367_4eebb82d")
    assert bots._sid_from_conflict(text) == "api_1793981367_4eebb82d"
    assert bots._sid_from_conflict("просто ошибка") == ""


def test_заголовок_сессии_бота_влезает_в_лимит_шлюза(rooms):
    """Заголовок комнаты бывает до MAX_TITLE, а шлюз режет сессию на 100:
    длинный заголовок давал голый 500 «Server got itself in trouble»."""
    long_title = ("Очень длинный заголовок комнаты, где боты обсуждают "
                  "архитектуру проекта и не только, " * 3)
    room = bots.create_room("default", long_title[:bots.MAX_TITLE], "тема",
                            _team())
    assert len(room["title"]) > 60
    for persona in ("default", "bot-architect", "bot-critic"):
        title = bots._session_title(room, persona)
        assert len(title) <= bots._GW_TITLE_MAX, (len(title), title)
        assert title.startswith("bots · ")
        assert persona in title and room["id"] in title
        assert title.endswith("[%s]" % room["id"])
    # Короткий заголовок не обрезается — уходит в шлюз как есть.
    short = _room()
    assert bots._session_title(short, "critic") == \
        "bots · План · critic [%s]" % short["id"]


def test_clip_не_превышает_лимит():
    text = "абвгд " * 1000
    for limit in (0, 5, 12, 60, 1200, bots.MAX_TURN_CHARS):
        clipped = bots._clip(text, limit)
        assert len(clipped) <= limit, (limit, len(clipped))


def test_id_сессии_сохраняется_сразу(rooms):
    room = bots.load_room("default", _room()["id"])
    bots._remember_sid(room, "default", "api_42")
    got = bots.load_room("default", room["id"])
    assert got["sessions"]["default"]["sid"] == "api_42"


def test_ask_persona_забирает_занятую_сессию_и_идёт_в_неё(rooms, monkeypatch):
    """Повтор после сбоя: сессия уже создана, но её id не был сохранён."""
    room = bots.load_room("default", _room()["id"])
    from routes import agentai_bridge as bridge
    created = []

    class Resp:
        def __init__(self, status, data=None):
            self.status_code = status
            self.content = b"{}" if data is not None else b""
            self._data = data if data is not None else {}

        def json(self):
            return self._data

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return Resp(404, {"error": {"message": "нет сессии"}})

        async def post(self, url, headers=None, json=None):
            if url.endswith("/api/sessions"):
                created.append(url)
                return Resp(409, {"error": {"message":
                    "Title 'x' is already in use by session api_7"}})
            assert url.endswith("/api/sessions/api_7/chat")
            return Resp(200, {"message": {"content": "готово"}})

    import types
    monkeypatch.setattr(bridge, "_hermes", lambda: object())
    monkeypatch.setattr(bridge, "_gw_base", lambda launch, persona: "http://gw")
    monkeypatch.setattr(bridge, "_profile_key", lambda launch, persona: "k")
    monkeypatch.setattr(bridge, "httpx", types.SimpleNamespace(
        Timeout=lambda **kw: None, AsyncClient=lambda timeout: Client()))

    text, sid = asyncio.run(
        bots.ask_persona(room, {"persona": "default"}, "тема"))
    assert (text, sid) == ("готово", "api_7")
    assert len(created) == 1
    saved = bots.load_room("default", room["id"])["sessions"]["default"]["sid"]
    assert saved == "api_7"


def test_авто_идёт_до_лимита(rooms, monkeypatch):
    room = _room()
    calls = []
    monkeypatch.setattr(bots, "ask_persona", _fake_ask(calls))
    asyncio.run(bots._worker("default", room["id"], 3, 0.0))
    got = bots.load_room("default", room["id"])
    assert [r["index"] for r in got["rounds"]] == [1, 2, 3]
    assert len([m for m in got["messages"] if m["kind"] == "bot"]) == 6
    # В следующем круге бот видит реплику соседа из этого же круга.
    assert "Круг 2" in calls[2]["message"]


def test_стоп_между_репликами_обрывает_круг(rooms, monkeypatch):
    room = _room()

    async def ask(room_obj, bot, message):
        bots._STOP.add(room_obj["id"])
        return "ответ " + bot["persona"], "sid"
    monkeypatch.setattr(bots, "ask_persona", ask)
    asyncio.run(bots._worker("default", room["id"], 5, 0.0))
    got = bots.load_room("default", room["id"])
    assert len(got["rounds"]) == 1
    assert got["rounds"][0]["interrupted"] is True
    assert len([m for m in got["messages"] if m["kind"] == "bot"]) == 1
    assert got["status"] == "idle"


def test_стоп_во_время_хода_помечает_круг_прерванным(rooms, monkeypatch):
    """Отмена задачи посреди хода: круг не должен оставаться «висячим»."""
    room = _room()

    async def ask(room_obj, bot, message):
        if bot["persona"] == "architect":
            await asyncio.sleep(30)
        return "ответ " + bot["persona"], "sid"
    monkeypatch.setattr(bots, "ask_persona", ask)

    async def run():
        task = asyncio.create_task(bots._worker("default", room["id"], 1, 0.0))
        while not bots.load_room("default", room["id"])["messages"]:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())

    got = bots.load_room("default", room["id"])
    assert got["rounds"][0]["interrupted"] is True
    assert got["rounds"][0]["finished"]
    assert got["rounds"][0]["ok"] == 1
    assert got["status"] == "idle"
    assert got["auto"] is None


def test_реплика_пользователя_попадает_в_контекст(rooms, monkeypatch):
    room = _room()
    bots.say("default", room["id"], "Начните с рисков")
    calls = []
    monkeypatch.setattr(bots, "ask_persona", _fake_ask(calls))
    asyncio.run(bots._worker("default", room["id"], 1, 0.0))
    got = bots.load_room("default", room["id"])
    assert got["messages"][0]["kind"] == "user"
    assert got["messages"][0]["round"] == 1
    assert "Начните с рисков" in calls[0]["message"]


def test_реплика_во_время_круга_видна_следующему_боту(rooms, monkeypatch):
    room = _room()

    async def ask(room_obj, bot, message):
        if bot["persona"] == "critic":
            bots.say("default", room_obj["id"], "Не трогайте миграции")
        else:
            assert "Не трогайте миграции" in message
        return "ответ " + bot["persona"], "sid-" + bot["persona"]
    monkeypatch.setattr(bots, "ask_persona", ask)
    asyncio.run(bots._worker("default", room["id"], 1, 0.0))
    got = bots.load_room("default", room["id"])
    assert any(m["kind"] == "user" and m["round"] == 1
               for m in got["messages"])


def test_чужая_комната_не_видна(rooms):
    room = _room(pid="alpha")
    with pytest.raises(bots.BotsError) as err:
        bots.load_room("beta", room["id"])
    assert err.value.status == 404
    assert bots.list_rooms("beta") == []


def test_плохой_id_отвергается(rooms):
    with pytest.raises(bots.BotsError):
        bots.load_room("default", "../config")


# --------------------------------------------------------------------------
# Инжекция панели в страницу агента: она имеет смысл только на origin
# приложения и только при включённом рубильнике.
# --------------------------------------------------------------------------
_PAGE = b"<html><head><title>Agent</title></head><body>hi</body></html>"


def _bridge():
    from hermes_module.integration import bridge
    return bridge


def test_панель_ботов_не_инжектится_без_флага(monkeypatch):
    bridge = _bridge()
    import module_flags
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": False)
    assert bridge._inject_bots(_PAGE, "default") == _PAGE


def test_панель_ботов_инжектится_один_раз(monkeypatch):
    bridge = _bridge()
    import module_flags
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": True)
    once = bridge._inject_bots(_PAGE, "default")
    assert b"agentai_bots.js" in once and b"agentai_bots.css" in once
    assert once.count(b"infinity-bots-bridge") == 1
    assert bridge._inject_bots(once, "default") is once


def test_панель_ботов_не_трогает_ответ_без_head(monkeypatch):
    bridge = _bridge()
    import module_flags
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": True)
    body = b'{"sessions": []}'
    assert bridge._inject_bots(body, "default") == body


# --------------------------------------------------------------------------
# Рубильник проверяется в коде маршрутов, а не только в интерфейсе. На живой
# машине у всех профилей флаг включён пользователем, поэтому ветку «выключен»
# проверяем прямо на обработчике.
# --------------------------------------------------------------------------
def _request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": "/api/agentai/bots/rooms",
                    "headers": [], "query_string": b"", "scheme": "http",
                    "server": ("127.0.0.1", 8281), "client": ("127.0.0.1", 1)})


def _api():
    from routes import agentai_bots as api
    return api


def test_выключенный_флаг_даёт_403(monkeypatch, rooms):
    api = _api()
    import module_flags
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": False)
    resp = api.bots_rooms(_request())
    assert resp.status_code == 403
    assert "agentai_bots" in resp.body.decode("utf-8")
    # статус всегда 200: панели нужно знать про рубильник до остальных запросов
    resp = api.bots_status(_request())
    assert resp.status_code == 200 and b'"enabled":false' in resp.body


def test_включённый_флаг_отдаёт_комнаты(monkeypatch, rooms):
    api = _api()
    import module_flags
    monkeypatch.setattr(module_flags, "enabled_for", lambda m, p="": True)
    room = _room()
    resp = api.bots_rooms(_request())
    assert resp.status_code == 200
    assert json.loads(resp.body.decode("utf-8"))["result"][0]["id"] == room["id"]


def test_старые_штампы_иконок_пересчитываются_при_чтении(rooms, monkeypatch):
    """Комната хранит иконку, снятую при создании: после смены палитры
    (эмодзи → Font Awesome) старые комнаты показывали бы старое."""
    palette = {"critic": "fa-scale-balanced", "architect": "fa-compass"}
    monkeypatch.setattr(bots, "icon_of", lambda p: palette.get(p, "fa-robot"))
    room = _room()
    path = bots._room_path("default", room["id"])
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    for bot in raw["bots"]:
        bot["icon"] = "🔮"
    raw["messages"] = [{"id": 1, "kind": "bot", "bot": "critic", "round": 1,
                        "text": "привет", "ts": 0, "icon": "🔮"}]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False)

    loaded = bots.load_room("default", room["id"])
    assert [b["icon"] for b in loaded["bots"]] == ["fa-scale-balanced", "fa-compass"]
    assert loaded["messages"][0]["icon"] == "fa-scale-balanced"
    assert [b["icon"] for b in bots.list_rooms("default")[0]["bots"]] == \
        ["fa-scale-balanced", "fa-compass"]
    # Чтение не переписывает файл комнаты.
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["bots"][0]["icon"] == "🔮"
