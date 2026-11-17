"""Утечки медиа между профилями: список, мини-плеер, очередь команд, handoff.

Репорт пользователя 04.11: песня, загруженная в профиле test1, была видна в
списке песен профиля test2. Замер показал три разных механизма, и каждый здесь
закреплён отдельно:

1. профиль без своих каталогов получал выдачу по всей таблице (sql_filter не
   ставил условие) — новый/пустой профиль видел чужую загруженную музыку;
2. мини-плеер искал трек по всей таблице и без точного профиля рассылал
   команду всем открытым страницам;
3. очередь /api/media/player-commands была общей на процесс: страница одного
   профиля исполняла команду «play», отправленную для другого.

Клиентское состояние плеера (media.playback/media.resume/miniplayer) живёт в
localStorage общего origin — его разделяет префикс профиля, который сервер
кладёт в HTML (webui_assets.inject_profile_prefix); проверяем сам сниппет.
"""
import asyncio
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import pytest

import media_visibility as MV


PUBLIC = {"music": ["C:/pub/music"], "video": ["C:/pub/video"]}
PRIVATE = {
    "test1": {"music": ["D:/test1/music"], "video": ["D:/test1/video"]},
    "test2": {"music": [], "video": []},
}
PROFILE_IDS = ["default", "test1", "test2"]


def _effective(profile_id):
    pid = profile_id or "default"
    if pid not in PROFILE_IDS:
        return {}
    own = PRIVATE.get(pid, {"music": [], "video": []})
    return {"media": {
        "music_dirs": list(PUBLIC["music"]) + own["music"],
        "video_dirs": list(PUBLIC["video"]) + own["video"],
    }}


@pytest.fixture()
def profiles(monkeypatch):
    import profiles_module as PM
    monkeypatch.setattr(MV, "_effective", lambda profile_id=None: _effective(profile_id))
    # «Сырые» конфиги и пути общедоступного профиля тоже подменяем: иначе
    # all_indexable_dirs подмешает каталоги реальной машины.
    monkeypatch.setattr(MV, "_raw_profile_config", lambda profile_id: {})
    monkeypatch.setattr(PM, "get_public_paths", lambda: {"music": list(PUBLIC["music"]),
                                                         "video": list(PUBLIC["video"])},
                        raising=False)
    monkeypatch.setattr(MV, "_resolve", lambda p: str(p).replace("\\", "/").rstrip("/"))
    monkeypatch.setattr(PM, "list_profiles", lambda: [{"id": pid} for pid in PROFILE_IDS])
    monkeypatch.setattr(PM, "get_default_profile_id", lambda: "default")
    monkeypatch.setattr(PM, "get_active_id", lambda: "test1")
    MV.invalidate()
    return MV


def test_profile_without_own_dirs_sees_only_public(profiles):
    """Общедоступный профиль test2 не должен видеть D:/test1/music."""
    cond, params = MV.sql_filter("music", "test2")
    assert cond not in (None, "", "0")
    assert all("test1" not in str(p) for p in params), params
    assert MV.stream_allowed("D:/test1/music/song.mp3", "test2") is False
    assert MV.stream_allowed("C:/pub/music/song.mp3", "test2") is True


def test_profile_with_no_dirs_at_all_sees_nothing(profiles, monkeypatch):
    """Пустой профиль — не «показываем всё», а «не показываем ничего»."""
    monkeypatch.setattr(MV, "_effective", lambda profile_id=None: {"media": {}})
    MV.invalidate()
    assert MV.visible_dirs("music", "test2") == []
    assert MV.sql_filter("music", "test2") == ("0", [])
    # файл вне библиотек всех профилей — легальный локальный поток (торрент);
    # закрытые чужие каталоги проверены выше, при живых конфигах
    assert MV.stream_allowed("C:/Users/you/Downloads/fresh.mkv", "test2") is True


class _Rec:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, list(params)))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


def test_player_search_track_is_scoped(profiles, monkeypatch):
    import media_module as MM
    import player_api

    rec = _Rec()
    monkeypatch.setattr(MM, "get_db", lambda: rec)

    player_api._search_track(track_id="track-1", profile="test2")
    player_api._search_track(query="Skindred", profile="test2")
    player_api._search_track(profile="test2")   # последний прослушанный

    by_id, by_query, last_played = [q[0] for q in rec.queries]
    assert " LIKE ? ESCAPE" in by_id and " AND (" in by_id, by_id
    assert " LIKE ? ESCAPE" in by_query and " AND (" in by_query, by_query
    # history общая: фильтр стоит по пути трека (m.filepath), а не по id
    assert "m.filepath LIKE ? ESCAPE" in last_played, last_played
    for sql, params in rec.queries:
        assert params, (sql, params)


def test_broadcast_reaches_only_own_profile(monkeypatch):
    import player_api

    sent = {"test1": [], "test2": []}

    class _Q:
        def __init__(self, pid):
            self.pid = pid

        def put_nowait(self, item):
            sent[self.pid].append(item)

    player_api._subscribers.clear()
    player_api._subscribers["a"] = {"profile": "test1", "queue": _Q("test1")}
    player_api._subscribers["b"] = {"profile": "test2", "queue": _Q("test2")}
    try:
        delivered = player_api._broadcast({"id": "c1", "action": "open",
                                           "profile": "test1", "ts": 0})
    finally:
        player_api._subscribers.clear()
    assert delivered == 1
    assert [c["id"] for c in sent["test1"]] == ["c1"]
    assert sent["test2"] == []


def test_command_without_profile_goes_to_public(monkeypatch):
    """Центр действий без профиля не должен искать по всей библиотеке."""
    import player_api

    seen = {}

    def fake_search(track_id="", query="", filepath="", profile=""):
        seen["profile"] = profile
        return None

    monkeypatch.setattr(player_api, "_search_track", fake_search)
    cmd = player_api.MiniCommand(action="open", track_id="x")
    with pytest.raises(player_api.HTTPException):
        asyncio.run(player_api.execute_command(cmd, ""))
    assert seen["profile"] == "default", seen


class _Req:
    def __init__(self, profile, method="GET", since=0, body=None):
        self.headers = {"x-test-profile": profile}
        self.query_params = {"since": str(since)}
        self._body = body or {}

    async def json(self):
        return self._body


def _media_routes():
    try:
        from routes import legacy_media_player as mp
    except Exception as exc:   # монолит не собирается без части зависимостей
        pytest.skip("server_backup не импортируется: %s" % exc)
    return mp


def test_player_command_queue_is_per_profile(monkeypatch):
    mp = _media_routes()
    monkeypatch.setattr(mp, "_profile_id_of",
                        lambda req: req.headers.get("x-test-profile") or "default")
    mp._player_cmds["queue"].clear()
    mp._player_cmds["seq"] = 0

    asyncio.run(mp.api_media_player_command(_Req("test1", "POST", body={"action": "pause"})))
    asyncio.run(mp.api_media_player_command(_Req("test2", "POST", body={"action": "next"})))

    mine = mp.api_media_player_commands(_Req("test1"))
    other = mp.api_media_player_commands(_Req("test2"))
    assert [c["action"] for c in mine["commands"]] == ["pause"]
    assert [c["action"] for c in other["commands"]] == ["next"]
    # since двигается только по своим командам: чужие id не «съедают» окно
    after = mp.api_media_player_commands(_Req("test1", since=mine["latest"]))
    assert after["commands"] == []


def test_profile_prefix_snippet_namespaces_storage():
    import webui_assets as W

    html = "<html><head><title>x</title></head><body></body></html>"
    out = W.inject_profile_prefix(html, "test2")
    assert "isearch:test2:" in out
    assert out.index("__PROFILE_STORAGE_PREFIX") < out.index("</head>")
    # повторная инъекция не дублирует скрипт
    assert W.inject_profile_prefix(out, "test1").count("__PROFILE_STORAGE_PREFIX") == 1
    # общедоступный профиль остаётся на старых ключах — его прогресс не теряется
    public = W.inject_profile_prefix(html, "default")
    assert 'isearch:default:' not in public
