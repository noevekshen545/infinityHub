"""Проверки живости агента: без сети к мёртвому порту и с узнаванием своей сборки.

Живой замер 04.11: `/api/fm-ai-tasks/status` отвечал 2.7 с, пока шлюз агента
был не поднят — health() делал два urlopen к закрытым портам и каждый висел
полный таймаут 2 с (на Windows SYN к закрытому порту не получает RST).

Вторая проверка — переносимая сборка `hermes\\HermesAgent.exe`: её командная
строка не содержит ни «hermes_cli», ни пути модуля, поэтому после рестарта
приложения живой агент считался посторонним.
"""
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from hermes_module.integration import launch  # noqa: E402


def test_health_не_ходит_в_сеть_без_слушателя(monkeypatch):
    """Нет слушателя — HTTP-проба не запускается вовсе (иначе 2 с таймаута)."""
    monkeypatch.setattr(launch, "_net_table", lambda: [])
    monkeypatch.setattr(launch, "_alive", lambda proc: False)
    monkeypatch.setattr(launch, "_gw_proc", None)
    monkeypatch.setattr(launch, "_ui_proc", None)

    def forbidden(*a, **k):
        raise AssertionError("HTTP-проба при закрытом порте")

    monkeypatch.setattr(launch, "_http_ok", forbidden)
    res = launch.health()
    assert res["gateway_running"] is False
    assert res["webui_running"] is False
    assert res["gateway_http"] is False
    assert res["webui_http"] is False


class _Conn:
    def __init__(self, port, pid):
        self.status = "LISTEN"
        self.laddr = type("Addr", (), {"port": port})()
        self.pid = pid


def test_владелец_порта_из_общего_снимка(monkeypatch):
    """health ищет оба порта в одном снимке таблицы, а не читает её дважды."""
    calls = []

    def table():
        calls.append(1)
        return [_Conn(8642, 111), _Conn(9120, 222)]

    monkeypatch.setattr(launch, "_net_table", table)
    assert launch._port_owner(8642, table()) == 111
    assert len(calls) == 1
    assert table() and launch._port_owner(9999, table()) is None


def test_портативный_агент_узнаётся_по_замороженному_exe(monkeypatch, tmp_path):
    root = tmp_path
    module_dir = os.path.join(str(root), "app", "hermes_module")
    os.makedirs(module_dir)
    monkeypatch.setattr(launch.config, "MODULE_DIR", module_dir)
    frozen = os.path.join(str(root), "hermes", "HermesAgent.exe")

    monkeypatch.setattr(launch, "_proc_tree_text",
                        lambda pid: f"{frozen.lower()}|gateway|run")
    assert launch._pid_is_agent(8642, 111) is True

    monkeypatch.setattr(launch, "_proc_tree_text",
                        lambda pid: "d:\\other\\hermes\\hermesagent.exe|gateway|run")
    assert launch._pid_is_agent(8642, 111) is False
