"""
Hermes Agent (Infinity Isolated) — интеграционный слой в Infinity Search.

Исходники агента лежат в hermes_module/{backend,frontend,.hermes-portable} и
НЕ принадлежат слою интеграции: обновление апстрима их не трогает.
Здесь только связывание с приложением.

Состав:
  config.py      — пути, порты, mount-путь, интерпретатор
  launch.py      — жизненный цикл процессов (gateway + web UI): start/stop/health
  bridge.py      — FastAPI-роутер: HTTP + WebSocket + SSE прокси на web UI агента
  provider.py    — прописывает агенту движок приложения (/openai/v1) и модель
  voice_bridge.py— перенаправляет TTS/STT агента на движок проекта
  app.json       — карточка модуля для каталога приложений

Подключение в main.py (паттерн прокси-модулей проекта):
    from hermes_module.integration.bridge import router as hermes_router
    app.include_router(hermes_router)
"""

import os

# Каталог модуля (runjson/hermes_module/) и корень приложения (runjson/)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNJSON = os.path.dirname(HERE)

__all__ = ["config", "launch", "bridge", "provider", "voice_bridge"]
