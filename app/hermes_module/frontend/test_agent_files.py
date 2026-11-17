"""Тест эндпоинтов файлов инструкций агента: /api/agent/*.

Запуск:  python -m frontend.test_agent_files
Главное здесь — безопасность: произвольный путь приниматься не должен.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontend import web_server as ws  # noqa: E402


class FakeTransport:
    def __init__(self, host: str):
        self._host = host

    def get_extra_info(self, _name):
        return (self._host, 12345)


class FakeRequest:
    """Минимальный aiohttp.Request: нужны только query, json и transport."""

    def __init__(self, query=None, body=None, host="127.0.0.1"):
        self.query = query or {}
        self._body = body if body is not None else {}
        self.transport = FakeTransport(host)

    async def json(self):
        return self._body


FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    if not cond:
        FAILS += 1
    mark = "OK   " if cond else "FAIL "
    print(mark + label + ((" — " + detail) if detail and not cond else ""))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agentfiles_"))
    ws.AGENT_FILES_DIR = tmp
    ws.AGENT_FILES = {"SOUL.md": "Личность", "USER.md": "Данные", "AGENTS.md": "Инструкции"}

    # --- список ---
    r = await ws.handle_agent_files(FakeRequest())
    check("GET /api/agent/files отдаёт 200", r.status == 200)
    data = json.loads(r.text)
    names = [f["name"] for f in data["files"]]
    check("список содержит все файлы из AGENT_FILES", names == list(ws.AGENT_FILES), str(names))
    check("несуществующие файлы помечены exists=False",
          all(f["exists"] is False for f in data["files"]))

    # --- чтение ---
    r = await ws.handle_agent_file_read(FakeRequest(query={"name": "SOUL.md"}))
    check("чтение отсутствующего файла не падает",
          json.loads(r.text).get("exists") is False)
    # Записываем бинарно: текстовый режим на Windows переводит \n в \r\n,
    # и сравнение с «\n» падало бы не из-за бага сервера, а из-за платформы.
    (tmp / "SOUL.md").write_bytes("Я — помощник\n".encode("utf-8"))
    r = await ws.handle_agent_file_read(FakeRequest(query={"name": "SOUL.md"}))
    check("чтение отдаёт содержимое",
          json.loads(r.text).get("content") == "Я — помощник\n")

    # --- ЗАЩИТА: имя вне белого списка ---
    for bad in ("../../windows/system32/drivers/etc/hosts",
                "../../evil.md", "SOUL.md.bak", "SOUL", ""):
        r = await ws.handle_agent_file_read(FakeRequest(query={"name": bad}))
        check("чтение %r отклонено" % bad, r.status == 400, r.text[:80])
        r = await ws.handle_agent_file_write(FakeRequest(body={"name": bad, "content": "x"}))
        check("запись %r отклонена" % bad, r.status == 400, r.text[:80])

    # --- ЗАЩИТА: только loopback ---
    far = FakeRequest(host="10.0.0.5")
    check("список недоступен не с этой машины",
          (await ws.handle_agent_files(far)).status == 403)
    check("чтение недоступно не с этой машины",
          (await ws.handle_agent_file_read(FakeRequest(query={"name": "SOUL.md"}, host="10.0.0.5"))).status == 403)
    check("запись недоступна не с этой машины",
          (await ws.handle_agent_file_write(FakeRequest(body={"name": "SOUL.md", "content": "x"}, host="10.0.0.5"))).status == 403)

    # --- запись ---
    r = await ws.handle_agent_file_write(FakeRequest(body={"name": "SOUL.md", "content": "Новый текст"}))
    check("запись возвращает 200", r.status == 200, r.text[:120])
    check("файл записан на диск",
          (tmp / "SOUL.md").read_bytes() == "Новый текст".encode("utf-8"))
    check("создана копия .bak", (tmp / "SOUL.md.bak").is_file())
    check("временный файл убран", not (tmp / "SOUL.md.tmp").exists())

    # --- некорректные данные ---
    r = await ws.handle_agent_file_write(FakeRequest(body={"name": "SOUL.md", "content": 123}))
    check("content не строка отклонён", r.status == 400, r.text[:80])
    big = "x" * (ws.AGENT_FILE_MAX_BYTES + 10)
    r = await ws.handle_agent_file_write(FakeRequest(body={"name": "SOUL.md", "content": big}))
    check("слишком большой файл отклонён", r.status == 400, r.text[:80])
    check("файл не испорчен отклонённой записью",
          (tmp / "SOUL.md").read_bytes() == "Новый текст".encode("utf-8"))

    # --- содержимое ---
    text = "Привет, агент!\nСтрока два\n"
    await ws.handle_agent_file_write(FakeRequest(body={"name": "USER.md", "content": text}))
    check("кириллица записалась без потерь",
          (tmp / "USER.md").read_bytes() == text.encode("utf-8"))
    check("в списке только .md",
          all(n.lower().endswith(".md") for n in ws.AGENT_FILES), str(list(ws.AGENT_FILES)))

    print()
    print("провалов: %d" % FAILS if FAILS else "все проверки пройдены")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
