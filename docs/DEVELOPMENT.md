[Обзор](../README.md) · [Система](SYSTEM.md) · [Расширения](EXTENSIONS.md) · [Установка](INSTALL.md) · [Руководство](USER-GUIDE.md) · [Модули](MODULES.md) · [FAQ](FAQ.md)

# Разработка

## Архитектура

```
Браузер / Electron
        │  HTTP + WebSocket
        ▼
app/main.py  ── FastAPI + uvicorn, один процесс
        │
        ├── app/routes/      HTTP-маршруты (71 файл)
        ├── app/modules/     модули предметной области
        ├── app/server/      сборка приложения и конфиг
        ├── app/legacy/      вынесенные секции старого монолита
        ├── app/templates/   89 HTML-страниц
        └── app/static/      CSS, JS, библиотеки
        │
        ├── engine/          llama.cpp (внешний бинарник)
        ├── models/          gguf, whisper, Silero, Piper
        ├── data/            профили, индексы, кэш
        └── app/hermes_module/  интеграция Hermes Agent
```

`app/main.py` — точка входа. Он собирает FastAPI-приложение, подключает маршруты,
запускает фоновые задачи и отдаёт интерфейс. Код рассчитан на запуск как скрипт:
`python main.py --server`, а не на импорт.

## Структура репозитория

| Путь | Назначение |
|---|---|
| `app/` | бэкенд, шаблоны, статика, тесты |
| `app/routes/` | HTTP-маршруты |
| `app/modules/` | модульная логика (книги, заметки, медиа, обучение) |
| `app/legacy/` | исторические секции, вынесенные из монолита |
| `app/server/` | конфиг, безопасность, сборка приложения |
| `desktop/` | Electron-оболочка |
| `modules/avi/` | Rust-крейт Avi |
| `scripts/` | bootstrap и сборка release-ассетов |
| `docs/` | документация |
| `tools/` | QA-скрипты и замеры |

## Запуск из исходников

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

cd app
python main.py --server --port 8281
```

Или:

```bat
run-dev.bat
```

`run-dev.bat` выставляет `INFINITY_ROOT`, `INFINITY_DATA_DIR` и запускает сервер.

### Переменные окружения

| Переменная | Назначение |
|---|---|
| `INFINITY_ROOT` | корень сборки |
| `INFINITY_DATA_DIR` | каталог данных |
| `INFINITY_ENGINE_DIR` | каталог движка |
| `INFINITY_MODELS_DIR` | каталог моделей |
| `INFINITY_SAFE_MODE=1` | не исполнять расширения при старте |
| `INFINITY_STRICT_BIND=1` | падать при несвязанных именах в legacy-модулях |
| `INFINITY_ACCESS_LOG=1` | полный журнал доступа |
| `OPENROUTER_API_KEY` | ключ внешнего провайдера |

## Тесты

```bash
cd app
python -m pytest tests -q
```

Быстрая проверка синтаксиса:

```bash
python -m compileall -q app modules tools
```

Часть тестов требует ComfyUI, Calibre, Hermes или живого сервера — они помечены
`skip`, если компонент отсутствует.

## Сборка портативной версии

1. Соберите зависимости и приложение:
   - Python-часть — PyInstaller (`InfinitySearch.exe` + `_internal/`);
   - Electron — `electron-builder` в `desktop/`.
2. Соберите `modules/avi` через Cargo.
3. Положите рядом `python/`, `engine/`, `app/`, `desktop/`, `hermes/`.
4. Проверьте `start.bat` и `start-desktop.bat`.
5. Соберите архив и приложите к GitHub Release.

Готовый архив не должен содержать `data/`, `logs/`, `trash/`, `_backups/`, `_shots/`,
`.git/` и пользовательские профили.

## Release-ассеты

Тяжёлые компоненты не хранятся в Git. Скрипт `scripts/pack-assets.ps1` создаёт:

```
release-assets/
├─ engine.zip   # llama.cpp: llama-server.exe + DLL
├─ hermes.zip   # backend + frontend Hermes
└─ README.txt
```

Затем файлы прикрепляются к GitHub Release. Пользователи скачивают их через
`bootstrap --engine-url/--hermes-url`.

## Соглашения

- Python 3.12, UTF-8, отступ 4 пробела.
- Не добавлять пользовательские данные и секреты в Git.
- Новые модули — в `app/modules/` или `app/routes/`, регистрация в `app/server/`.
- Для необязательных зависимостей — `try/except ImportError` и понятное сообщение.
- Тесты — в `app/tests/`, без обращения к реальным профилям.
- Большие бинарники — только через Releases.
- Расширения — по правилам из [`EXTENSIONS.md`](EXTENSIONS.md).

## Известные ограничения

- Основная платформа — Windows.
- Локальный движок — кастомная сборка llama.cpp; апстрим может не поддерживать
  часть флагов (`--fit`, `--spec-type draft-mtp` и другие).
- Hermes требует отдельный Python 3.11–3.13.
- ComfyUI, Calibre, VLC и модели ставятся отдельно.
