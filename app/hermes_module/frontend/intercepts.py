"""Перехват сообщений командами без ИИ (Настройки → Команды).

Команда-перехватчик ловит фразу пользователя ДО отправки агенту и выполняет
заранее написанный скрипт. Модель, агент и режим «Диалог» при этом не
вызываются вообще — ни одного запроса к шлюзу не уходит.

Здесь живёт только серверная часть: выполнение python-скрипта в отдельном
процессе с таймаутом и ограничением вывода. Хранение команд, сопоставление
фраз и запуск JS выполняет фронтенд (frontend/assets/js/11-intercepts.js).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
try:
    import swallow                      # внутри приложения: след в общий лог
except ImportError:
    # модуль умеет работать и вне приложения — тогда следа нет, но и падения нет
    class swallow:
        @staticmethod
        def note(*a, **k):
            pass

# Ограничения выполнения. Скрипт пишет пользователь, поэтому потолок небольшой:
# это защита от случайного «while True: print(...)», а не от вредного кода.
INTERCEPT_TIMEOUT_MAX = 300
INTERCEPT_TIMEOUT_DEFAULT = 60
INTERCEPT_MAX_OUT = 200_000
INTERCEPT_MAX_CODE = 512 * 1024
INTERCEPT_MAX_FILE = 512 * 1024
# Скрипт по пути читаем только из этих расширений: иначе через «читалку»
# можно было бы вытащить содержимое любого файла (ключей, конфигов и т.п.).
INTERCEPT_FILE_EXT = (".py", ".js", ".mjs", ".txt", ".ps1", ".bat", ".cmd", ".sh")


def _path_allowed(path: Path) -> str | None:
    """Проверить путь к файлу скрипта. Возвращает текст ошибки или None.

    Файл должен существовать, быть обычным файлом (не каталогом и не ссылкой
    в обход проверки) и иметь разрешённое расширение. Содержимое отдаётся
    наружу целиком, поэтому проверка строгая.
    """
    try:
        # Раскрываем ~ и переменные окружения до проверки, иначе обойти можно было бы.
        p = Path(os.path.expandvars(str(path))).expanduser()
    except Exception:
        swallow.note('intercepts._path_allowed:L51')
        return "Некорректный путь"
    if p.suffix.lower() not in INTERCEPT_FILE_EXT:
        return "Допустимы только скрипты: " + ", ".join(INTERCEPT_FILE_EXT)
    try:
        if not p.exists():
            return "Файл не найден: " + str(p)
        if not p.is_file():
            return "Это не обычный файл: " + str(p)
        if p.stat().st_size > INTERCEPT_MAX_FILE:
            return "Файл слишком большой"
    except OSError as e:
        swallow.note('intercepts._path_allowed:L63', e)
        return f"Не удалось открыть файл: {e}"
    return None


def read_script(path: str) -> dict:
    """Прочитать файл скрипта для POST /api/intercept/read. Исключений не бросает."""
    err = _path_allowed(path)
    if err:
        return {"error": err}
    try:
        text = Path(os.path.expandvars(str(path))).expanduser().read_text(
            encoding="utf-8", errors="replace")
    except OSError as e:
        swallow.note('intercepts.read_script:L77', e)
        return {"error": f"Не удалось прочитать файл: {e}"}
    return {"code": text, "path": str(path)}


def _exec_env() -> dict:
    """Окружение с вырезанными секретами — то же, что у консольных команд."""
    try:
        from tools.environments.local import build_subprocess_env
        return build_subprocess_env()
    except Exception:
        swallow.note('intercepts._exec_env:L88')
        return os.environ.copy()


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            swallow.note('intercepts._decode:L97')
            continue
    return raw.decode("utf-8", errors="replace")


def _win_flags() -> int:
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags
        return windows_hide_flags()
    except Exception:
        swallow.note('intercepts._win_flags:L107')
        return 0


def run_intercept(code: str, cwd: str, timeout: int, args: str = "") -> dict:
    """Выполнить python-код в подпроцессе. Исключений не бросает.

    Код пишется во временный файл и запускается интерпретатором сервера.
    Стандартный ввод получает аргументы командной строки одной строкой
    (JSON), чтобы скрипт мог их разобрать через sys.argv[1].
    """
    if not cwd or not Path(cwd).is_dir():
        cwd = str(Path.cwd())

    payload = args or ""
    # Превращаем «{...}» в валидный JSON-объект, иначе скрипту нечего будет parsed.
    if args.strip().startswith("{"):
        try:
            json.loads(args)
        except Exception:
            swallow.note('intercepts.run_intercept:L127')
            payload = json.dumps({"raw": args}, ensure_ascii=False)

    fd, path = tempfile.mkstemp(prefix="infinity_intercept_", suffix=".py")
    t0 = time.time()
    code_run = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        proc = subprocess.run(
            [sys.executable, path, payload],
            cwd=cwd, env=_exec_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, creationflags=_win_flags(),
        )
        out = _decode(proc.stdout or b"")
        code_run = int(proc.returncode or 0)
    except subprocess.TimeoutExpired as e:
        swallow.note('intercepts.run_intercept:L145', e)
        out = _decode(e.stdout or b"") + f"\n[!] скрипт остановлен по таймауту {timeout} с"
        code_run = 124
    except Exception as e:
        swallow.note('intercepts.run_intercept:L149', e)
        out = f"[!] не удалось выполнить скрипт: {e}"
        code_run = 127
    finally:
        try:
            os.unlink(path)
        except OSError:
            swallow.note('intercepts.run_intercept:L156')
            pass

    truncated = len(out) > INTERCEPT_MAX_OUT
    if truncated:
        out = out[:INTERCEPT_MAX_OUT] + "\n… [вывод усечён]"
    return {"code": code_run, "out": out,
            "dur": round(time.time() - t0, 2), "truncated": truncated}


def validate(payload: dict) -> tuple[dict | None, str | None]:
    """Проверить и нормализовать тело POST /api/intercept/run."""
    if not isinstance(payload, dict):
        return None, "Некорректный запрос"
    code = str(payload.get("code") or "")
    if not code.strip():
        return None, "Пустой скрипт"
    if len(code) > INTERCEPT_MAX_CODE:
        return None, "Скрипт слишком большой"
    try:
        timeout = max(1, min(INTERCEPT_TIMEOUT_MAX,
                             int(payload.get("timeout") or INTERCEPT_TIMEOUT_DEFAULT)))
    except (TypeError, ValueError):
        swallow.note('intercepts.validate:L179')
        timeout = INTERCEPT_TIMEOUT_DEFAULT
    cwd = str(payload.get("cwd") or "").strip()
    if not cwd or not Path(cwd).is_dir():
        cwd = str(Path.cwd())
    args = str(payload.get("args") or "")
    if len(args) > 32_768:
        args = args[:32_768]
    return {"code": code, "cwd": cwd, "timeout": timeout, "args": args}, None
