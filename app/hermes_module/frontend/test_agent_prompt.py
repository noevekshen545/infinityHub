"""Проверка /api/agent/prompt: инструкции должны переживать «перезапуск»."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from frontend import web_server as ws  # noqa: E402


class T:
    def __init__(self, host):
        self.h = host

    def get_extra_info(self, _n):
        return (self.h, 1)


class R:
    def __init__(self, method="GET", body=None, host="127.0.0.1"):
        self.method = method
        self._b = body or {}
        self.transport = T(host)
        self.query = {}

    async def json(self):
        return self._b


async def main():
    d = Path(tempfile.mkdtemp(prefix="prompt_"))
    ws.AGENT_PROMPT_FILE = d / "AGENT_PROMPT.md"

    r = await ws.handle_agent_prompt(R())
    print("до записи exists:", json.loads(r.text)["exists"])

    text = "Ты инженер-ассистент, отвечай кратко."
    r = await ws.handle_agent_prompt(R("POST", {"content": text}))
    print("запись:", r.status)

    r = await ws.handle_agent_prompt(R())
    data = json.loads(r.text)
    print("после чтения exists:", data["exists"], "совпадает:", data["content"] == text)
    print("бэкап создан:", (d / "AGENT_PROMPT.md.bak").is_file())

    print("content не строка:", (await ws.handle_agent_prompt(R("POST", {"content": 123}))).status)
    print("не с этой машины:",
          (await ws.handle_agent_prompt(R("GET", None, "10.0.0.5"))).status)

    # Пустой текст — валидное значение: инструкции можно очистить
    r = await ws.handle_agent_prompt(R("POST", {"content": ""}))
    print("очистка:", r.status, "файл:", ws.AGENT_PROMPT_FILE.is_file())


if __name__ == "__main__":
    asyncio.run(main())
