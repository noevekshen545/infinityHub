"""
GenLab — AI-генератор изображений, видео, аудио и 3D-моделей на базе ComfyUI.

Самодостаточный FastAPI-модуль InfinitySearch. Подключается в main.py через
``app.include_router(genlab_router)`` — обязательно ДО catch-all роута
``@app.api_route("/{path_name:path}")``, иначе запросы уйдут в legacy-обработчик.

Доступ:
    UI      → http://<host>:<port>/genlab
    API     → http://<host>:<port>/api/genlab/*
    WebSocket → ws://<host>:<port>/api/genlab/ws

Архитектура:
    config.py       — настройки и портативный резолв путей
    comfy_client.py — HTTP + WebSocket клиент ComfyUI
    service.py      — процесс ComfyUI, очередь, реестр job'ов
    graph.py        — сборка API-prompt и валидация по /object_info
    workflows/      — пресеты (чистые функции: params -> dict графа)
    store.py        — SQLite (WAL): галерея, генерации, коллекции
    catalog.py      — каталог моделей + что установлено
    api.py          — APIRouter

Модуль чисто библиотечный: main.py запрещает импорт не как __main__, поэтому
никаких побочных эффектов на уровне импорта здесь быть не должно.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"