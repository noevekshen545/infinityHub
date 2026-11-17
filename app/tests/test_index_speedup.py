"""Тесты ускорения индексации: потоки, батч-коммиты, fail-fast сети.

media_module на импорте поднимает Flask, БД и сетевую авто-валидацию индекса
(один из прошлых прогонов pytest висел именно на 8 SYN_SENT к lyrics.ovh),
поэтому проверяемые хелперы вырезаются из исходника через AST и исполняются в
подставном пространстве имён с заглушками config_module и socket. Структура
циклов индексации проверяется по дереву разбора — это ровно то, что должно
остаться на месте после правок.

Замер, из которого взяты константы: tools/_index_cost_probe.py (ffprobe 58.5 мс
на файл; 4 потока — 71 тр/с, 8 — 170, 16 — 270, 24 — 310, 32 — 311).
"""
import ast
import os
import sys
import time
import types
from urllib.parse import urlparse

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)
SRC = os.path.join(APP, "media_module.py")
PHOTO_SRC = os.path.join(APP, "photo_module.py")

import swallow

WANTED_FUNCS = {"_media_cfg_section", "_media_flag", "_index_workers",
                "_net_ok", "_net_ok_url"}
WANTED_CONSTS = {"_INDEX_WORKERS_DEFAULT", "_INDEX_WORKERS_MAX", "_INDEX_COMMIT_EVERY",
                 "_NET_LATCH_TTL", "_NET_CONNECT_TIMEOUT", "_NET_LATCH", "_NET_LATCH_LOCK"}


def _module_nodes(path):
    with open(path, encoding="utf-8-sig") as f:
        return ast.parse(f.read()).body


class _Sock:
    """Сокет-заглушка как контекстный менеджер: `with` ищет методы по типу, а не по экземпляру."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """Заглушка socket: считает пробы и либо «соединяется», либо бросает ошибку."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def create_connection(self, addr, timeout=None):
        self.calls.append((addr, timeout))
        if not self.ok:
            raise OSError("нет маршрута")
        return _Sock()


def _load(monkeypatch, media=None, conn=None):
    """Вырезать хелперы из media_module и исполнить их с подставным конфигом и сокетом."""
    cfg = {"media": dict(media or {})}
    stub_cfg = types.ModuleType("config_module")
    stub_cfg.get_config = lambda: cfg
    monkeypatch.setitem(sys.modules, "config_module", stub_cfg)
    monkeypatch.setitem(sys.modules, "socket", conn or _Conn(ok=False))

    body = []
    for n in _module_nodes(SRC):
        if isinstance(n, ast.FunctionDef) and n.name in WANTED_FUNCS:
            body.append(n)
        elif isinstance(n, ast.Assign):
            names = [t.id for t in n.targets if isinstance(t, ast.Name)]
            if any(x in WANTED_CONSTS for x in names):
                body.append(n)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.target.id in WANTED_CONSTS:
                body.append(n)

    ns = {"os": os, "time": time, "urlparse": urlparse,
          "threading": __import__("threading"),
          "swallow": swallow,
          "logger": types.SimpleNamespace(info=lambda *a, **k: None,
                                          debug=lambda *a, **k: None,
                                          warning=lambda *a, **k: None)}
    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    ns["_cfg"] = cfg
    ns["_latch"] = ns["_NET_LATCH"]
    return ns


def _func(name, path=SRC):
    for n in _module_nodes(path):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError("функция %s не найдена в %s" % (name, path))


def _walk(node, kind):
    return [x for x in ast.walk(node) if isinstance(x, kind)]


def _calls(node, fname):
    return [c for c in _walk(node, ast.Call)
            if isinstance(c.func, ast.Name) and c.func.id == fname]


def _commits(node):
    return [c for c in _walk(node, ast.Call)
            if isinstance(c.func, ast.Attribute) and c.func.attr == "commit"]


def test_default_workers_raised_from_eight_to_measured_plateau(monkeypatch):
    ns = _load(monkeypatch)
    assert ns["_INDEX_WORKERS_DEFAULT"] == 16
    expected = max(2, min(16, os.cpu_count() or 4))
    assert ns["_index_workers"]() == expected


def test_config_overrides_workers_and_clamps(monkeypatch):
    ns = _load(monkeypatch, media={"index_workers": 4})
    assert ns["_index_workers"]() == 4
    ns["_cfg"]["media"] = {"index_workers": 1000}
    assert ns["_index_workers"]() == ns["_INDEX_WORKERS_MAX"] == 32
    ns["_cfg"]["media"] = {"index_workers": 0}
    assert ns["_index_workers"]() == max(2, min(16, os.cpu_count() or 4))
    ns["_cfg"]["media"] = {"index_workers": "не число"}
    assert ns["_index_workers"]() >= 2


def test_media_flag_still_reads_the_same_section(monkeypatch):
    ns = _load(monkeypatch, media={"fetch_lyrics": True, "auto_index": False})
    assert ns["_media_flag"]("fetch_lyrics") is True
    assert ns["_media_flag"]("auto_index") is False
    # как и до правки: флаг всегда bool, отсутствующий ключ даёт default
    assert ns["_media_flag"]("чего_нет") is False
    assert ns["_media_flag"]("чего_нет", 1) is True
    assert ns["_media_flag"]("auto_index", True) is False


def test_net_ok_denies_unreachable_host_once_per_ttl(monkeypatch):
    conn = _Conn(ok=False)
    ns = _load(monkeypatch, conn=conn)
    for _ in range(3):
        assert ns["_net_ok"]("api.lyrics.ovh") is False
    # три обращения = одна проба: иначе каждый трек платит таймаут
    assert len(conn.calls) == 1
    assert conn.calls[0][0] == ("api.lyrics.ovh", 443)
    assert conn.calls[0][1] == ns["_NET_CONNECT_TIMEOUT"] <= 2.0


def test_net_ok_caches_positive_answer_too(monkeypatch):
    conn = _Conn(ok=True)
    ns = _load(monkeypatch, conn=conn)
    assert ns["_net_ok"]("ru.wikipedia.org") is True
    assert ns["_net_ok"]("ru.wikipedia.org") is True
    assert len(conn.calls) == 1


def test_net_ok_probe_is_no_longer_silent(monkeypatch):
    """Отказ сети больше не «тихий» except: точка пишет след в swallow."""
    conn = _Conn(ok=False)
    ns = _load(monkeypatch, conn=conn)
    swallow.reset()
    assert ns["_net_ok"]("api.lyrics.ovh") is False
    got = swallow.stats()
    assert any(k.startswith("media_module._net_ok:") for k in got), sorted(got)
    # повтор той же точки не плодит строк в логе: у неё один растущий счётчик
    ns["_NET_LATCH"].clear()
    ns["_net_ok"]("api.lyrics.ovh")
    assert [v for k, v in swallow.stats().items()
            if k.startswith("media_module._net_ok:")] == [2]


def test_net_ok_probe_expires_after_ttl(monkeypatch):
    conn = _Conn(ok=False)
    ns = _load(monkeypatch, conn=conn)
    ns["_net_ok"]("api.lyrics.ovh")
    ns["_latch"]["api.lyrics.ovh"] = (False, time.monotonic() - 1.0)
    ns["_net_ok"]("api.lyrics.ovh")
    assert len(conn.calls) == 2


def test_net_ok_url_uses_host_and_scheme_and_skips_junk(monkeypatch):
    conn = _Conn(ok=True)
    ns = _load(monkeypatch, conn=conn)
    assert ns["_net_ok_url"]("https://image.tmdb.org/t/p/w500/x.jpg") is True
    assert ns["_net_ok_url"]("http://example.com/a.jpg") is True
    assert conn.calls[-1][0] == ("example.com", 80)
    before = len(conn.calls)
    assert ns["_net_ok_url"]("") is False
    assert ns["_net_ok_url"]("это не url") is False
    assert len(conn.calls) == before


def test_pools_use_configurable_workers(monkeypatch):
    for fn in ("index_music_directories", "index_video_directories"):
        body = _func(fn)
        pools = [c for c in _walk(body, ast.Call)
                 if isinstance(c.func, ast.Name) and c.func.id == "ThreadPoolExecutor"]
        assert pools, "%s: пул потоков не найден" % fn
        for p in pools:
            kw = {k.arg: k.value for k in p.keywords}
            assert isinstance(kw.get("max_workers"), ast.Name), \
                "%s: max_workers обязан браться из workers = _index_workers()" % fn
            assert kw["max_workers"].id == "workers"
        assert _calls(body, "_index_workers"), "%s: _index_workers() не вызывается" % fn


def test_index_loops_commit_periodically_and_keep_final_commit():
    """Коммит раз в N записей вместо единственного в конце прогона."""
    for fn in ("index_music_directories", "index_video_directories"):
        body = _func(fn)
        found = []
        for if_node in _walk(body, ast.If):
            test = if_node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "inserted_since_commit" and _commits(if_node)):
                found.append(test.comparators[0])
        assert found, "%s: промежуточного коммита по счётчику нет" % fn
        assert len(_commits(body)) >= 2, "%s: пропал финальный commit()" % fn


def test_lyrics_phase_requires_reachable_host_and_short_timeout():
    body = _func("_enrich_music_track")
    guards = _calls(body, "_net_ok")
    assert guards, "сетевой fail-fast для текстов песен не подключён"
    assert any(c.args and getattr(c.args[0], "value", None) == "api.lyrics.ovh" for c in guards)
    assert _calls(body, "_media_flag"), "media.fetch_lyrics перестал уважаться"
    lyr = _func("search_lyrics_online")
    timeouts = [k.value.value for c in _walk(lyr, ast.Call) for k in c.keywords
                if k.arg == "timeout" and isinstance(k.value, ast.Constant)]
    assert timeouts and max(timeouts) <= 5, "ожидание ответа lyrics.ovh всё ещё длинное"


def test_video_phase_skips_unreachable_network_hosts():
    body = _func("index_video_directories")
    hosts = [getattr(c.args[0], "value", None) for c in _calls(body, "_net_ok")]
    assert "ru.wikipedia.org" in hosts, "сетевые описания фильмов не защищены fail-fast"
    assert _calls(body, "_net_ok_url"), "скачивание постеров не защищено fail-fast"


def test_photo_scan_batches_commits_but_still_flushes():
    body = _func("scan_photos", PHOTO_SRC)
    conds = [c for c in _walk(body, ast.Compare)
             if isinstance(c.left, ast.Name) and c.left.id == "since_commit"]
    assert conds, "батч-коммит в скане фото не найден"
    flushes = [i for i in _walk(body, ast.If)
               if isinstance(i.test, ast.Name) and i.test.id == "since_commit"]
    assert flushes, "нет финального сброса хвостовой пачки фото"
    # было по commit() на каждое фото; стало: счётчик + сброс + этап удаления
    assert len(_commits(body)) <= 6, "в скане фото осталось слишком много commit()"
