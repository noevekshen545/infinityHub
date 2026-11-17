"""Infinity CLI menu and launcher — runs Hermes subcommands with 'infinity' branding."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_HOME = ROOT / ".hermes-portable"
BACKEND = ROOT / "backend"

COMMANDS = [
    ("Диагностика и система", [
        ("status", "infinity status", "Статус всех компонентов системы"),
        ("logs", "infinity logs", "Логи агента (последние 50 строк)"),
        ("logs -f", "infinity logs -f", "Следить за логами в реальном времени"),
        ("logs errors", "infinity logs errors", "Только ошибки"),
        ("doctor", "infinity doctor", "Проверка установки и окружения"),
        ("verify", "infinity verify", "Проверка конфигурации и запуск тестов"),
        ("security", "infinity security", "Аудит безопасности зависимостей"),
        ("approvals", "infinity approvals", "Правила подтверждения опасных действий"),
        ("insights", "infinity insights", "Статистика и аналитика использования"),
        ("monitoring", "infinity monitoring", "Мониторинг шлюза и здоровье"),
        ("prompt-size", "infinity prompt-size", "Размер системного промпта и инструментов"),
        ("journey", "infinity journey", "Журнал прогресса и этапов"),
        ("checkpoints", "infinity checkpoints", "Контрольные точки сессий"),
        ("dump", "infinity dump", "Дамп сводки системы для отладки"),
        ("debug", "infinity debug", "Инструменты отладки и отправка логов"),
        ("console", "infinity console", "Безопасная интерактивная консоль"),
        ("completion", "infinity completion", "Генерация скрипта автодополнения"),
    ]),
    ("Сессии и память", [
        ("sessions", "infinity sessions", "Управление сессиями (меню)"),
        ("sessions list", "infinity sessions list", "Список всех сессий"),
        ("sessions browse", "infinity sessions browse", "Интерактивный выбор сессии"),
        ("memory", "infinity memory", "Внешняя память и долговременный контекст"),
        ("kanban", "infinity kanban", "Канбан-доска задач"),
        ("project", "infinity project", "Управление проектами и папками"),
        ("portal", "infinity portal", "Подключение к Nous Portal"),
        ("sync", "infinity sync", "Синхронизация навыков между устройствами"),
    ]),
    ("Модели и ключи", [
        ("model", "infinity model", "Выбор модели и провайдера"),
        ("fallback", "infinity fallback", "Резервные провайдеры при сбоях"),
        ("moa", "infinity moa", "Mixture of Agents конфигурация"),
        ("proxy", "infinity proxy", "Локальный OpenAI-прокси"),
        ("egress", "infinity egress", "Egress-фаервол для изоляции"),
        ("secrets", "infinity secrets", "Хранилища секретов (Bitwarden/1Password)"),
        ("auth", "infinity auth", "Пул учетных данных провайдеров"),
        ("login", "infinity login", "Вход в аккаунт провайдера"),
        ("logout", "infinity logout", "Сброс авторизации провайдера"),
        ("vault", "infinity vault", "Локальное зашифрованное хранилище"),
    ]),
]
COMMANDS.extend([
    ("Инструменты и навыки", [
        ("tools", "infinity tools", "Конфигурация доступных инструментов"),
        ("skills", "infinity skills", "Поиск, установка и управление навыками"),
        ("bundles", "infinity bundles", "Наборы навыков (бандлы)"),
        ("plugins", "infinity plugins", "Управление плагинами"),
        ("curator", "infinity curator", "Фоновый куратор и очистка навыков"),
        ("mcp", "infinity mcp", "Управление MCP-серверами"),
        ("lsp", "infinity lsp", "Языковые серверы (LSP)"),
        ("computer-use", "infinity computer-use", "Управление рабочим столом (CUA)"),
        ("hooks", "infinity hooks", "Скрипты-хуки жизненного цикла"),
    ]),
    ("Шлюз и каналы", [
        ("gateway status", "infinity gateway status", "Статус фонового шлюза"),
        ("gateway run", "infinity gateway run", "Запуск шлюза в консоли"),
        ("gateway start", "infinity gateway start", "Старт службы шлюза"),
        ("gateway stop", "infinity gateway stop", "Остановка службы шлюза"),
        ("gateway restart", "infinity gateway restart", "Перезапуск службы шлюза"),
        ("gateway list", "infinity gateway list", "Список профилей шлюза"),
        ("gateway setup", "infinity gateway setup", "Настройка каналов сообщений"),
        ("cron", "infinity cron", "Планировщик фоновых задач"),
        ("webhook", "infinity webhook", "Управление вебхуками"),
        ("send", "infinity send", "Отправка сообщения в канал"),
        ("pairing", "infinity pairing", "Коды сопряжения устройств"),
        ("peer", "infinity peer", "Связь между шлюзами Hermes"),
        ("whatsapp", "infinity whatsapp", "Настройка WhatsApp"),
        ("whatsapp-cloud", "infinity whatsapp-cloud", "Настройка WhatsApp Cloud API"),
        ("slack", "infinity slack", "Настройка интеграции Slack"),
        ("browser", "infinity browser", "Управление профилем браузера"),
        ("pets", "infinity pets", "Анимированные питомцы Petdex"),
    ]),
    ("Интерфейсы и запуск", [
        ("start_infinity", "infinity start", "Запустить Infinity Web UI + Gateway"),
        ("chat", "infinity chat", "Чат с агентом в терминале"),
        ("dashboard", "infinity dashboard", "Веб-дашборд Hermes"),
        ("serve", "infinity serve", "Headless-сервер Hermes"),
        ("desktop", "infinity desktop", "Запуск десктопного приложения"),
        ("acp", "infinity acp", "Запуск в режиме ACP-агента"),
    ]),
])
COMMANDS.extend([
    ("Управление и профили", [
        ("config", "infinity config", "Просмотр текущей конфигурации"),
        ("config edit", "infinity config edit", "Редактировать config.yaml"),
        ("profile", "infinity profile", "Управление профилями изоляции"),
        ("skin", "infinity skin", "Темы и стили оформления"),
        ("pause", "infinity pause", "Экстренная пауза всех задач"),
        ("resume", "infinity resume", "Снятие экстренной паузы"),
        ("import", "infinity import", "Импорт бэкапа из zip"),
        ("import-agent", "infinity import-agent", "Импорт агента из Claude Code / Codex"),
        ("migrate", "infinity migrate", "Миграция устаревших настроек"),
        ("worktree", "infinity worktree", "Очистка временных git worktree"),
        ("claw", "infinity claw", "Пакеты расширений OpenClaw"),
        ("backup", "infinity backup", "Создать резервную копию"),
        ("update", "infinity update", "Обновить Hermes Agent"),
        ("uninstall", "infinity uninstall", "Удаление компонентов Hermes"),
    ]),
])


def _build_flat_list():
    flat = []
    for _group_title, items in COMMANDS:
        for cmd, label, desc in items:
            flat.append((cmd, label, desc))
    return flat


def run_command(cmd_str: str, extra_args: list = None):
    extra = list(extra_args or [])
    if cmd_str == "start_infinity":
        bat = ROOT / "start_infinity.bat"
        print(f"\n[infinity] Запуск: {bat}\n")
        subprocess.run(["cmd.exe", "/c", str(bat)], check=False)
        return

    parts = shlex.split(cmd_str) + extra
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"\n[infinity] hermes {' '.join(parts)}\n")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main"] + parts,
            cwd=str(BACKEND),
            env=env,
            check=False,
        )
        print(f"\n[infinity] Код возврата: {proc.returncode}")
    except KeyboardInterrupt:
        print("\n[infinity] Прервано пользователем")


def print_list():
    print("=" * 68)
    print("  INFINITY - Список команд (выполняются как hermes ...)")
    print("=" * 68)
    idx = 1
    for group_title, items in COMMANDS:
        print(f"\n  --- {group_title} ---")
        for _cmd, label, desc in items:
            print(f"  {idx:2d}. {label:<28} — {desc}")
            idx += 1
    print("\n   0. Выход\n")


def interactive_menu():
    flat = _build_flat_list()
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print("=" * 68)
        print("  INFINITY - Панель команд Hermes")
        print(f"  HERMES_HOME: {HERMES_HOME}")
        print("=" * 68)

        idx = 1
        for group_title, items in COMMANDS:
            print(f"\n  --- {group_title} ---")
            for _cmd, label, desc in items:
                print(f"  {idx:2d}. {label:<28} — {desc}")
                idx += 1

        print("\n   0. Выход\n")
        try:
            choice = input("Номер команды или своя команда: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nВыход.")
            break

        if not choice or choice in ("0", "q", "exit", "quit"):
            break

        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(flat):
                cmd_tuple = flat[n - 1]
                run_command(cmd_tuple[0])
            else:
                print(f"[!] Неверный номер: {n}")
        else:
            c = choice
            if c.lower().startswith("infinity "):
                c = c[9:].strip()
            elif c.lower().startswith("hermes "):
                c = c[7:].strip()
            run_command(c)

        input("\nНажмите Enter для возврата в меню...")


def main():
    args = sys.argv[1:]
    if not args:
        interactive_menu()
        return

    first = args[0].lower()
    if first in ("--list", "-l", "list"):
        print_list()
        return
    if first in ("--help", "-h", "help"):
        print("""Использование:
  infinity.bat                   - интерактивное меню всех команд
  infinity.bat --list            - вывести список всех доступных команд
  infinity.bat <команда> [опции] - выполнить команду напрямую
      Примеры:
        infinity.bat status
        infinity.bat logs -f
        infinity.bat gateway status
        infinity.bat model
        infinity.bat start_infinity
""")
        return

    run_command(args[0], args[1:])


if __name__ == "__main__":
    main()


