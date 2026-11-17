"""Проверка импортов окружения агента."""
import sys
print("python   :", sys.version.split()[0])
print("executable:", sys.executable)
print("prefix   :", sys.prefix)
print("site     :", [p for p in sys.path if "site-packages" in p])

missing = []
for m in ["openai", "yaml", "aiohttp", "fastapi", "pydantic", "psutil",
          "rich", "jinja2", "httpx", "websockets", "tenacity", "fire",
          "croniter", "pathspec", "markdown", "jwt", "PIL"]:
    try:
        __import__(m)
    except Exception as e:
        missing.append(f"{m} ({type(e).__name__}: {str(e)[:70]})")
print("MISSING  :", missing if missing else "нет")
