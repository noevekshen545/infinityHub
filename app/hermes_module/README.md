# Hermes Agent — модуль Infinity Search

AI-агент **Hermes Agent 0.21.5** (сборка «Infinity Isolated») встроен в Infinity
Search как полноценный модуль: поднимается вместе с системой, живёт на её же
движке и использует её же голос. Старый агент ASIO отключён.

Исходники агента (Hermes) и Electron-оболочка **не трогаются апстридом** —
всё связывание вынесено в `integration/`.

---

## Как это выглядит

```
Браузер ──► /hermes ──► main.py (bridge.py) ──► web UI агента (127.0.0.1:9120)
                            │                          │
                            │                          └──► Hermes gateway (8642)
                            │                                 │
                            ├── /openai/v1 ──► движок системы (llama.cpp / LM Studio)
                            └── /hermes/api/voice/{tts,stt} ──► speech_engine проекта
```

Агент запускается **в WSL**, в своём venv, и слушает только `127.0.0.1` —
наружу смотрит только прокси main.py.

## Структура

| Путь | Что это |
|---|---|
| `backend/` | Hermes Agent (апстрим, ~7500 файлов) |
| `frontend/` | Infinity Web UI агента (апстрим + локальный `ai_engine_api.py`) |
| `.hermes-portable/` | перенесённый профиль агента (только как источник при первом запуске) |
| `integration/` | **связывание с системой** — единственное, что правим мы |
| `integration/config.py` | пути, порты, флаги `hermes.*` из config.json |
| `integration/launch.py` | жизненный цикл процессов: start / stop / health / state.json |
| `integration/bridge.py` | FastAPI-роутер `/hermes`: HTTP + WebSocket + SSE, адаптеры голоса |
| `integration/provider.py` | прописывает агенту движок системы (`/openai/v1`) и модель |
| `integration/voice_bridge.py` | конфигурация TTS/STTS-заголовков для прокси |
| `integration/webui.py` | локальный раннер web UI (вместо `0.0.0.0` из апстрима) |
| `app.json` | карточка модуля для каталога приложений |
| `setup_env.sh` | подготовка venv и зависимостей |
| `run_agent.sh` / `agent_ctl.py` | ручное управление (start/stop/status/logs) |
| `tests/` | проверки интеграции |
| `REMOVED.md` | что и как удалено/отключено, как откатить |

## Настройка (config.json)

```jsonc
"hermes": {
  "enabled": true,      // модуль подключён
  "autostart": true,    // поднимать вместе с main.py
  "mount": "/hermes",   // URL модуля
  "gateway_port": 8642,
  "ui_port": 9120
}
```

## Управление

```bash
bash hermes_module/setup_env.sh              # venv + зависимости (идемпотентно)
bash hermes_module/run_agent.sh status       # состояние
bash hermes_module/run_agent.sh start|stop|restart|logs
```

API модуля: `GET /hermes/api/status`, `POST /hermes/api/start|stop|restart`.

## Особенности, о которых стоит знать

**Отдельный интерпретатор.** Hermes требует Python `>=3.11,<3.14`, системный venv
проекта — 3.14. Поэтому модуль живёт в `~/.infinity/hermes-venv` (Python 3.13,
ставится через `uv`). Переопределяется `HERMES_INT_PYTHON`.

**Профиль на нативной ФС.** `HERMES_HOME = ~/.infinity/hermes-home`, а не
`hermes_module/.hermes-portable`. Агент использует SQLite в режиме WAL, а на
`/mnt/c` (9p/virtiofs) общая память WAL между процессами не когерентна — Hermes
прямо предупреждает о риске порчи БД. Каталог `.hermes-portable/` в репозитории —
эталонный профиль, из него профиль переносится при первом запуске.

**Голос — от системы.** В `requirements-module.txt` намеренно нет
`torch`/`torchaudio`/`faster-whisper`. Прокси добавляет агенту заголовки
`X-Voice-Engine-tts/stt` с адресами адаптеров `/hermes/api/voice/{tts,stt}`,
которые вызывают `speech_engine` проекта (silero/piper/qwen3 + faster-whisper).
Пользовательский движок, выбранный в UI агента, имеет приоритет и не перетирается.

**Прокси переписывает пути.** UI агента собран на корневых абсолютных путях
(`href="/assets/..."`, `fetch('/api/...')`, `new URL('/vendor/monaco/', ...)`).
За прокси на `/hermes` такие запросы ушли бы в корень основного приложения и
дали бы 404 — страница осталась бы без стилей и без связи с бэкендом. Поэтому
`bridge.py` в текстовых ответах (HTML/JS/CSS/JSON) подставляет префикс монтирования
в пути `/assets/`, `/vendor/`, `/api/`, `/v1/`, `/ws/`. Бинарные ответы не трогаются.

**Локальная правка апстрима.** В `frontend/ai_engine_api.py` сигнатуре
`autostart_on_boot()` добавлен параметр `_app` — иначе aiohttp >= 3.9 падает с
`TypeError` при старте web UI.

## Проверка

```bash
bash hermes_module/tests/verify.sh               # поднять агента и прогнать его
bash hermes_module/tests/verify_integration.sh    # эндпоинты в работающей системе
~/infinity-venv/bin/python3 hermes_module/tests/smoke_test.py
```

## Если агент не поднимается

1. `bash hermes_module/run_agent.sh status` — покажет, на каком шаге упало
2. `bash hermes_module/run_agent.sh logs` — логи в `hermes_module/logs/`
3. `bash hermes_module/setup_env.sh` — переустановить зависимости
4. Модуль не обязателен: при ошибке main.py стартует как обычно, `/hermes`
   покажет страницу с диагностикой. Выключить целиком — `"enabled": false`.
