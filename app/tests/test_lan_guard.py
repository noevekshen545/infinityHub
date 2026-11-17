"""Рубильник доступа по локальной сети.

Политика живёт в глобальном app/config.json (`server.lan_enabled`), поэтому
модуль можно импортировать напрямую: у него только stdlib и необязательный
psutil. Исполнение — в security_guard.check(), который тянет всё приложение,
поэтому для него проверяем исходник через ast: там важны три вещи — клиент
берётся из request.client.host (а не из _client_ip, который верит
X-Forwarded-For), решение возвращается 403, и собственная ошибка рубильника
не должна ломать выдачу.
"""
import ast
import json
import os
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import lan_guard


GUARD_SRC = os.path.join(APP, "security_guard.py")


@pytest.fixture
def cfg(tmp_path):
    """Конфиг во временной директории, с чистым мемо-состоянием модуля."""
    path = tmp_path / "config.json"
    old_path = lan_guard.CONFIG_PATH
    old_state = dict(lan_guard._state)
    old_counters = dict(lan_guard._counters)
    lan_guard.CONFIG_PATH = str(path)
    lan_guard._state.update({"enabled": True, "ts": 0.0, "loaded": False})
    lan_guard._counters.update({"denied": 0, "last_denied_ip": "",
                               "last_denied_path": "", "last_denied_ts": 0.0})
    try:
        yield path
    finally:
        lan_guard.CONFIG_PATH = old_path
        lan_guard._state.clear()
        lan_guard._state.update(old_state)
        lan_guard._counters.clear()
        lan_guard._counters.update(old_counters)


def write_cfg(path, server_section):
    path.write_text(json.dumps({"server": server_section, "other": {"keep": 1}},
                               ensure_ascii=False), encoding="utf-8")


# ---------------- loopback ----------------

@pytest.mark.parametrize("ip", ["127.0.0.1", "127.5.6.7", "::1", "::ffff:127.0.0.1", "localhost"])
def test_свой_компьютер_опознаётся(ip):
    assert lan_guard.is_loopback(ip) is True


@pytest.mark.parametrize("ip", ["192.168.1.40", "10.0.0.5", "172.20.3.9", "", None, "8.8.8.8"])
def test_чужой_адрес_не_петля(ip):
    assert lan_guard.is_loopback(ip) is False


# ---------------- решение пускать или нет ----------------

def test_включённый_рубильник_пускает_всех(cfg):
    write_cfg(cfg, {"lan_enabled": True})
    assert lan_guard.allow_decision("192.168.1.40") == (True, "")
    assert lan_guard._counters["denied"] == 0


def test_отсутствующий_ключ_означает_доступ_открыт(cfg):
    # Поведение «как до появления рубильника»: ничего не записано — сеть жива.
    write_cfg(cfg, {"host": "0.0.0.0"})
    assert lan_guard.lan_enabled() is True


def test_без_файла_конфига_доступ_не_ломается(cfg):
    assert lan_guard.lan_enabled() is True


def test_битый_файл_конфига_не_закрывает_сеть(cfg):
    cfg.write_text("{ это не json", encoding="utf-8")
    lan_guard.invalidate()
    assert lan_guard.lan_enabled() is True


def test_выключенный_рубильник_отклоняет_гостя_и_считает(cfg):
    write_cfg(cfg, {"lan_enabled": False})
    allow, why = lan_guard.allow_decision("192.168.1.40")
    assert allow is False
    assert "локальной сети" in why
    lan_guard.note_path("/api/media/music-index")
    assert lan_guard._counters["denied"] == 1
    assert lan_guard._counters["last_denied_ip"] == "192.168.1.40"
    assert lan_guard._counters["last_denied_path"] == "/api/media/music-index"


def test_выключенный_рубильник_не_отрезает_этот_ПК(cfg):
    write_cfg(cfg, {"lan_enabled": False})
    assert lan_guard.allow_decision("127.0.0.1")[0] is True
    assert lan_guard.allow_decision("::1")[0] is True
    assert lan_guard._counters["denied"] == 0


def test_петля_без_адреса_не_пропускается_как_гость(cfg):
    # request.client может отсутствовать; пустой адрес — не loopback, и при
    # закрытой сети он должен быть отклонён, а не «повезло».
    write_cfg(cfg, {"lan_enabled": False})
    assert lan_guard.allow_decision("")[0] is False


# ---------------- запись флага ----------------

def test_запись_флага_сохраняет_остальные_ключи(cfg):
    write_cfg(cfg, {"host": "0.0.0.0", "port": 8281})
    res = lan_guard.set_lan_enabled(False)
    assert res["ok"] is True
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["server"]["lan_enabled"] is False
    assert saved["server"]["port"] == 8281
    assert saved["other"] == {"keep": 1}


def test_запись_делает_страховочную_копию(cfg):
    write_cfg(cfg, {"host": "0.0.0.0"})
    lan_guard.set_lan_enabled(False)
    backup = cfg.parent / (cfg.name + ".lan-backup")
    assert backup.exists()
    assert "lan_enabled" not in json.loads(backup.read_text(encoding="utf-8"))["server"]


def test_запись_сразу_меняет_решение_без_ttl(cfg):
    write_cfg(cfg, {"lan_enabled": True})
    assert lan_guard.lan_enabled() is True
    lan_guard.set_lan_enabled(False)
    assert lan_guard.lan_enabled() is False
    assert lan_guard.allow_decision("192.168.1.40")[0] is False


def test_неудачная_запись_не_оставляет_полфайла(cfg):
    write_cfg(cfg, {"lan_enabled": True})
    lan_guard.set_lan_enabled(False)
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["server"]["lan_enabled"] is False
    assert list(cfg.parent.glob("*.lannet.tmp")) == []


def test_фолбэк_по_ttl_перечитывает_файл(cfg):
    write_cfg(cfg, {"lan_enabled": True})
    assert lan_guard.lan_enabled() is True
    # Сторонняя правка файла (например, руками) видна после истечения TTL.
    write_cfg(cfg, {"lan_enabled": False})
    lan_guard._state["ts"] = 0.0
    assert lan_guard.lan_enabled() is False


# ---------------- статус для панели ----------------

def test_статус_не_падает_без_psutil(cfg):
    write_cfg(cfg, {"lan_enabled": False})
    st = lan_guard.status(8281)
    assert st["lan_enabled"] is False
    assert st["port"] == 8281
    assert isinstance(st["urls"], list)
    assert st["denied_total"] == 0


def test_адреса_сети_даются_с_портом(cfg):
    urls = lan_guard.lan_addresses(8281)
    for u in urls:
        assert u["url"].endswith(":8281")
        assert not lan_guard.is_loopback(u["ip"])


def test_автоадрес_без_dhcp_не_показывается(monkeypatch):
    """169.254/8 никуда не маршрутизируется — в списке он только мешает."""
    import socket as _s
    import types

    class _Addr:
        def __init__(self, ip):
            self.family = _s.AF_INET
            self.address = ip

    fake = {"WLAN": [_Addr("127.0.0.1"), _Addr("192.168.0.11"), _Addr("169.254.11.22")],
            "Ethernet": [_Addr("169.254.99.1")]}
    mod = types.ModuleType("psutil")
    mod.net_if_addrs = lambda: fake
    monkeypatch.setitem(sys.modules, "psutil", mod)
    ips = [u["ip"] for u in lan_guard.lan_addresses(8281)]
    assert "192.168.0.11" in ips
    assert not [i for i in ips if i.startswith("169.254.")]


# ---------------- исполнение в guard ----------------

@pytest.fixture(scope="module")
def guard_gate():
    """Блок рубильника внутри security_guard.check() и его исходник.

    ast.dump не содержит исходных точек (request.client.host распадается в
    цепочку Attribute), поэтому для проверок «откуда берётся адрес» берём
    сегмент исходника узла.
    """
    with open(GUARD_SRC, encoding="utf-8-sig") as f:
        src = f.read()
    tree = ast.parse(src)
    check = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "check"), None)
    assert check is not None, "security_guard.check не найден"
    gates = [n for n in ast.walk(check) if isinstance(n, ast.Try) and any(
        isinstance(s, ast.Import) and any(a.name == "lan_guard" for a in s.names)
        for s in n.body)]
    assert len(gates) == 1, "блок рубильника не найден или их несколько: %s" % len(gates)
    return src, ast.get_source_segment(src, gates[0]), gates[0], check


def test_гейт_стоит_раньше_остальных_веток(guard_gate):
    """Рубильник должен решать до тарификаторов: гостю незачем накапливать бакеты."""
    _, _, gate, check = guard_gate
    after = [n.lineno for n in ast.walk(check)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") in
             ("_body_too_large", "_client_ip", "_is_trusted", "classify")]
    assert after, "в check() не найдены обычные ветки — тест потерял смысл"
    assert gate.lineno < min(after), "рубильник стоит после строки %s, а не до них" % min(after)


def test_клиент_берётся_из_request_client_a_ne_iz_xff(guard_gate):
    """_client_ip() верит X-Forwarded-For: гость выдал бы себя за 127.0.0.1."""
    _, segment, _, _ = guard_gate
    assert "request.client.host" in segment
    assert "_client_ip" not in segment


def test_отказ_возвращает_403_и_счётчик(guard_gate):
    _, segment, _, _ = guard_gate
    assert "allow_decision" in segment
    assert "denied_lan" in segment
    assert "403" in segment


def test_исключение_в_рубильнике_не_ломает_выдачу(guard_gate):
    """Если lan_guard упал, запрос идёт дальше по обычным веткам."""
    _, _, gate, _ = guard_gate
    assert len(gate.handlers) == 1
    handler = gate.handlers[0]
    assert getattr(handler.type, "id", "") == "Exception"
    assert any(isinstance(s, ast.Pass) for s in handler.body)


def test_статистика_знает_про_новый_счётчик(guard_gate):
    src, _, _, _ = guard_gate
    assert '"denied_lan": 0' in src
