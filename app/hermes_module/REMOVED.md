# Удалённые модули (30.09.2026)

## DeepSeek Harness (dsh) — удалён полностью
Снят с вооружения в пользу Hermes Agent (`/hermes`). Модуль физически
не собирался: `harnes/node_modules` был пуст, `is_built()` возвращал False,
ссылок в UI не было. Удалены:

    runjson/harnes/               — чек-аут DeepSeek Harness (~48 МБ, мёртвый)
    runjson/harnes_integration/    — слой интеграции dsh (роутер /finix)

Точки подключения в main.py убраны: `include_router(dsh_router)`
и `skip_inject` для `/finix`.

### Как вернуть (если понадобится)
1. Клонировать апстрим:  git clone https://github.com/deepseek-ai/deepseek-harness.git runjson/harnes
2. Вернуть слой интеграции (10 файлов) и две точки подключения в main.py:
   - `from harnes_integration.bridge import router as dsh_router; app.include_router(dsh_router)`
   - `skip_inject = path == "/finix" or path.startswith("/finix/")`
3. `pnpm install && pnpm run build` в runjson/harnes

## Legacy ASIO — отключён фиче-флагом (файлы на месте)
`asio_core.py`, `asio_engine.py`, `agent_module.py`, `agent_tools.py`,
`agent_*.py`, `routes/agent.py`, `core_plugins/`, `dreams_daemon.py`,
`AppDir/*` — всё осталось на диске.

Отключение: `config.json` → `agent.legacy_asio.enabled = false`.
При выключенном флаге `/api/agent/*` отвечает 410, страница `/agent`
редиректит на `/hermes`.

### Откат
Поставить `"enabled": true` в config.json и перезапустить main.py —
никаких других правок не требуется.
