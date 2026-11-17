"""Служебный скрипт обновления backend (запускает update_hermes.bat).

Вызывается РЕАЛЬНЫМ интерпретатором портативной сборки:

    hermes\\HermesAgent.exe -m integration.update_helper

Применяет локальные патчи Infinity (frontend/updater.py) и проверяет импорты
ключевых модулей агента в том окружении, в котором агент работает
(замороженные зависимости HermesAgent.exe + внешний backend/).
Код возврата 0 — обновление прошло успешно.
"""


def main() -> int:
    try:
        from frontend.updater import apply_local_patches
        n = apply_local_patches()
        print(f"[update] локальных патчей применено: {n}")
    except Exception as e:
        print(f"[update] НЕ УДАЛОСЬ применить локальные патчи: {type(e).__name__}: {e}")
        return 2

    try:
        import hermes_cli.main  # noqa: F401
        import gateway.run  # noqa: F401
        import gateway.platforms.api_server  # noqa: F401
        import agent.turn_response_intake  # noqa: F401
        print("[update] VERIFY_OK: hermes_cli.main / gateway.run / api_server / "
              "turn_response_intake импортируются")
        return 0
    except ModuleNotFoundError as e:
        print(f"[update] VERIFY_FAIL: не найден пакет '{e.name}'.")
        print("[update] Замороженные зависимости HermesAgent.exe устарели — "
              "пересоберите exe по build_portable\\hermes.spec с новыми "
              "зависимостями агента.")
        return 1
    except Exception as e:
        print(f"[update] VERIFY_FAIL: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
