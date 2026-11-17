# Запуск только веб-сервера (без gateway) — для проверки фронтенда плагинов.
#   python -m frontend.serve_frontend
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("INFINITY_PORT", "9131")
os.environ.setdefault("INFINITY_HTML", str(ROOT / "frontend" / "infinity.html"))

from frontend.web_server import run  # noqa: E402

if __name__ == "__main__":
    run()
