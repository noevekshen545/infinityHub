"""Видимость медиа по профилям: общедоступная библиотека + своё приватное.

Модель (решение пользователя 02.11): все видят библиотеку общедоступного
профиля и свои private_*-каталоги; чужое приватное не видит никто. Раньше
выдача делала `SELECT * FROM music_tracks` без фильтра, а prune удалял всё вне
списков сканировавшего профиля, поэтому проверяем три вещи:

1. WHERE по видимым префиксам попадает в реальные запросы выдачи;
2. маски в путях (`_`, `%`) экранируются — иначе чужой каталог пролезает;
3. prune считает «неактивной» директорию, которой нет ни у одного профиля,
   а не ту, которой нет у сканировавшего.

Конфиги профилей подменяем: тест не должен зависеть от того, какие каталоги
реально стоят на машине.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import media_visibility as MV


PUBLIC = {
    "music": ["C:/pub/music"],
    "video": ["C:/pub/video"],
    "photos": ["C:/pub/photos"],
}
PRIVATE = {
    "admin": {"music": ["D:/private_music"], "video": ["D:/private_vid"], "photos": []},
    "guest01": {"music": ["E:/other/_secret%dir"], "video": [], "photos": []},
    "default": {"music": [], "video": [], "photos": []},
}
PROFILE_IDS = ["default", "admin", "guest01"]


class FakeReq:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


def fake_effective(profile_id):
    """Как get_effective_config: база общедоступного + свои private_* поверх."""
    pid = profile_id or "default"
    if pid not in PROFILE_IDS:
        # несуществующий профиль: пустой конфиг, как у profiles_module
        return {}
    dirs = list(PUBLIC["music"])
    if pid == "admin":
        # у админа наследование снято: база пустая, живёт только своё
        dirs = []
    dirs = dirs + list(PRIVATE.get(pid, {}).get("music", []))
    vids = (list(PUBLIC["video"]) + list(PRIVATE.get(pid, {}).get("video", []))
            if pid != "admin" else list(PRIVATE["admin"]["video"]))
    return {"media": {"music_dirs": dirs, "video_dirs": vids,
                      "photo_dirs": list(PUBLIC["photos"])}}


def install_profiles(monkeypatch):
    """Подменить чтение конфигов и резолв путей: пути в тесте уже абсолютные."""
    import profiles_module as PM
    monkeypatch.setattr(MV, "_effective", lambda profile_id=None: fake_effective(profile_id))
    monkeypatch.setattr(MV, "_resolve", lambda p: str(p).replace("\\", "/").rstrip("/"))
    monkeypatch.setattr(PM, "list_profiles",
                        lambda: [{"id": pid} for pid in PROFILE_IDS])
    monkeypatch.setattr(PM, "get_default_profile_id", lambda: "default")
    monkeypatch.setattr(PM, "get_active_id", lambda: "admin")
    monkeypatch.setattr(PM, "request_profile",
                        lambda req: (req.cookies or {}).get(PM.COOKIE_NAME) or "default",
                        raising=False)
    MV.invalidate()
    return MV


def test_public_library_is_shared_and_private_is_own(monkeypatch):
    install_profiles(monkeypatch)
    assert MV.visible_dirs("music", "default") == ["C:/pub/music"]
    assert MV.visible_dirs("music", "guest01") == ["C:/pub/music", "E:/other/_secret%dir"]
    # снятое наследование: база уходит, своё остаётся
    assert MV.visible_dirs("music", "admin") == ["D:/private_music"]


def test_sql_filter_only_own_and_public(monkeypatch):
    install_profiles(monkeypatch)
    cond, params = MV.sql_filter("music", "default")
    assert len(params) == 1 and params[0].startswith("C:/pub/music/")
    cond, params = MV.sql_filter("music", "guest01")
    assert len(params) == 2


def test_sql_filter_escapes_wildcards_in_paths(monkeypatch):
    """Путь с `_` не должен становиться маской: иначе чужой каталог пролезает."""
    install_profiles(monkeypatch)
    _cond, params = MV.sql_filter("music", "guest01")
    secret = [p for p in params if "secret" in p][0]
    assert "\\_" in secret and "\\%" in secret, secret
    assert secret.startswith("E:/other/\\_secret\\%dir/")


def test_filter_off_returns_no_clause(monkeypatch):
    install_profiles(monkeypatch)
    base = fake_effective("default")
    monkeypatch.setattr(MV, "_effective", lambda pid=None: base)
    MV.FILTER_DEFAULT = True
    assert MV.sql_filter("music", "default")[0] is not None
    off = {"media": dict(base["media"], profile_filter="false")}
    monkeypatch.setattr(MV, "_effective", lambda pid=None: off)
    assert MV.sql_filter("music", "default") == (None, [])


def test_guest_without_cookie_gets_public_profile(monkeypatch):
    import profiles_module as PM
    install_profiles(monkeypatch)
    assert MV.profile_of_request(FakeReq()) == "default"
    assert MV.profile_of_request(FakeReq({PM.COOKIE_NAME: "admin"})) == "admin"


def test_union_for_prune_covers_every_profile(monkeypatch):
    install_profiles(monkeypatch)
    union = MV.all_indexable_dirs("music")
    assert "C:/pub/music" in union
    assert "D:/private_music" in union
    assert "E:/other/_secret%dir" in union
    assert len(union) == len({MV.norm(u) for u in union}), union


def test_stream_allowed_blocks_other_profiles_private(monkeypatch):
    install_profiles(monkeypatch)
    assert MV.stream_allowed("C:/pub/music/a.mp3", "default") is True
    assert MV.stream_allowed("D:/private_music/a.mp3", "default") is False
    assert MV.stream_allowed("D:/private_music/a.mp3", "admin") is True
    # файл вне библиотек всех профилей — легальный локальный поток
    assert MV.stream_allowed("C:/Users/you/downloads/fresh.mkv", "default") is True


def test_visibility_clause_fails_open(monkeypatch):
    """Сбой расчёта не должен ронять меди-страницу: фильтр просто не ставится."""
    install_profiles(monkeypatch)
    import media_module as MM

    def boom(*_a, **_k):
        raise RuntimeError("нет конфига")

    monkeypatch.setattr(MV, "sql_filter", boom)
    assert MM._visibility_clause("music", "default") == ("", [])


class _Rec:
    """Записывает запросы: проверка «фильтр попал в SQL» без настоящей БД."""

    def __init__(self):
        self.queries = []
        self._last = ""

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self._last = sql
        self.queries.append((sql, list(params)))
        return self

    def fetchall(self):
        if "DISTINCT filepath" in self._last:
            return [("C:/pub/music/a.mp3",)]
        if "DISTINCT directory" in self._last:
            return [("C:/pub/music",)]
        return []

    def fetchone(self):
        return [1]

    def close(self):
        pass


def test_music_index_data_filters_every_query(monkeypatch):
    install_profiles(monkeypatch)
    import media_module as MM
    rec = _Rec()
    monkeypatch.setattr(MM, "get_db", lambda: rec)
    out = MM.music_index_data(profile_id="default")
    sqls = [q[0] for q in rec.queries]
    assert sqls, "запросы не выполнены"
    # ни один запрос к таблице не остался без фильтра: total, строки и папки
    for s in sqls:
        if "music_tracks" in s:
            assert " WHERE " in s and "LIKE ? ESCAPE" in s, s
    assert out["total"] == 1
    from pathlib import Path
    assert out["directories"] == [str(Path("C:/pub/music/a.mp3").parent)], out["directories"]


def test_video_index_data_filters_every_query(monkeypatch):
    install_profiles(monkeypatch)
    import media_module as MM
    rec = _Rec()
    monkeypatch.setattr(MM, "get_db", lambda: rec)
    MM.video_index_data(profile_id="admin")
    for s, params in rec.queries:
        if "video_files" in s:
            assert " WHERE " in s and "is_valid = 1" in s, s
            assert any("private" in str(p) and "vid" in str(p) for p in params), (s, params)


def test_unknown_profile_sees_nothing(monkeypatch):
    """Стёртая/чужая кука не должна открывать всю таблицу."""
    install_profiles(monkeypatch)
    cond, params = MV.sql_filter("music", "net-takogo-profilya")
    assert cond == "0" and params == []
    assert MV.stream_allowed("D:/private_music/a.mp3", "net-takogo-profilya") is False


def test_statistics_aggregates_are_filtered_per_profile(monkeypatch):
    """Агрегаты «Статистики» считаются по видимому: иначе гость по суммарным
    размерам и жанрам восстанавливает чужую приватную библиотеку."""
    install_profiles(monkeypatch)
    import media_module as MM
    rec = _Rec()
    monkeypatch.setattr(MM, "get_db", lambda: rec)
    MM.get_media_statistics(profile_id="admin")
    tables = [(s, p) for s, p in rec.queries if "music_tracks" in s or "video_files" in s]
    assert any("music_tracks" in s for s, _ in tables), rec.queries
    assert any("video_files" in s for s, _ in tables), rec.queries
    for s, p in tables:
        assert " WHERE " in s and "LIKE ? ESCAPE" in s, s
    # у admin наследование общедоступного снято: в параметрах только его пути
    for _, p in tables:
        joined = " ".join(map(str, p))
        assert "D:/private" in joined, (p, joined)
        assert "pub" not in joined, (p, joined)


def test_prune_scope_is_union_not_scanned_profile(monkeypatch):
    install_profiles(monkeypatch)
    import media_module as MM
    scanned = MM._prune_prefixes("music", MV.visible_dirs("music", "admin"))
    # своё + чужое + общедоступное: прогон под admin не удалит чужие строки
    for want in ("C:/pub/music/", "D:/private_music/", "E:/other/_secret%dir/"):
        assert want in scanned, (want, scanned)


def test_route_serves_per_profile_and_caches_per_profile(monkeypatch):
    """Живой маршрут: кука → профиль → свои каталоги, и кэш не перетекает.

    Это проверка именно связки «запрос → профиль → фильтр → кэш на 30 с», а не
    только расчёта: общий кэш без ключа по профилю показал бы админу чужие
    папки (или наоборот) на втором запросе.
    """
    import profiles_module as PM
    install_profiles(monkeypatch)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import server
    from routes import legacy_common as lc
    from routes import legacy_media_player as mp

    calls = []

    def fake_index(limit=0, offset=0, genre=None, profile_id=None):
        calls.append(profile_id)
        dirs = MV.visible_dirs("music", profile_id)
        return {"tracks": [], "total": len(dirs), "directories": dirs,
                "indexed_at": 0}

    monkeypatch.setattr(server, "get_music_index", fake_index)
    lc._cache_invalidate("music_index")

    app = FastAPI()
    app.include_router(mp.router)
    client = TestClient(app)

    guest = client.get("/api/media/music-index?limit=0")
    admin = client.get("/api/media/music-index?limit=0",
                       headers={"Cookie": f"{PM.COOKIE_NAME}=admin"})
    again = client.get("/api/media/music-index?limit=0")

    assert guest.json()["directories"] == ["C:/pub/music"], guest.json()
    assert admin.json()["directories"] == ["D:/private_music"], admin.json()
    assert calls == ["default", "admin"], calls
    assert again.json()["directories"] == ["C:/pub/music"]
