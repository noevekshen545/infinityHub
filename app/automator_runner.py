"""Точка входа дочернего процесса-исполнителя макроса.

Запуск выглядит ровно так же, как запуск самого приложения (PyInstaller-сборка
принимает путь к .py первым аргументом):

    InfinitySearch.exe app\\automator_runner.py --macro <файл> --profile <id> --port 8281

Вынесено в процесс, а не в поток сервера, по двум причинам:
  * бесконечный цикл или тяжёлый numpy-шаг в макросе не должен вешать GIL сервера
    (у приложения уже есть профиль фризов: очередь за долгими запросами);
  * panic обязан догонять и потоки, которые макрос создал сам: трассировка их
    не видит, а TerminateProcess снимает весь процесс целиком.

Протокол: в stdout — строки JSON (status/log/line/result/error), из stdin —
команды (stop/pause/resume). Закрытый stdin означает, что сервер умер, — макрос
останавливается, а не остаётся висеть и печатать клавиши в чужую сессию.
"""

import argparse
import io
import json
import os
import sys
import threading
import time
import traceback
import swallow

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


_PROTOCOL = sys.stdout


def _emit(payload):
    """Одна строка JSON в канал протокола с немедленным сбросом: родитель читает потоково.

    Пишем в сохранённый stdout, а не в sys.stdout: перед исполнением макроса
    sys.stdout переводим в stderr, иначе протокол уехал бы вместе с print()
    пользователя, а родитель остался бы без единой строки результата.

    ensure_ascii=True — не мелочь: у собранного exe кодировка труб системная
    (замер 2026-10-31: cp1251), и русская строка «остановлено пользователем»
    прилетала байтами, которые родитель по UTF-8 не разобрал. ASCII-escape
    проходит через любую кодовую страницу.
    """
    _PROTOCOL.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
    _PROTOCOL.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description="исполнение макроса автоматизации")
    parser.add_argument("--macro", required=True, help="путь к .py макросу")
    parser.add_argument("--profile", default="", help="профиль-владелец макроса")
    parser.add_argument("--port", type=int, default=0, help="порт сервера для app.*")
    parser.add_argument("--args", default="{}", help="JSON параметров запуска")
    parser.add_argument("--dry", action="store_true", help="не вводить ничего")
    parser.add_argument("--hard-kill", type=float, default=2.0,
                        help="секунд до жёсткого выхода после stop")
    options = parser.parse_args(argv)

    import automator_input as A
    import automator_api as API
    import module_flags

    A.dpi_aware()
    if options.dry:
        A.set_dry(True)

    # Флаг проверяем и здесь, не только у родителя: файл раннера можно запустить
    # и из терминала, и выключенный модуль не должен тогда ничего вводить.
    if not module_flags.enabled_for("automator", options.profile):
        _emit({"event": "error", "text": "Модуль автоматизации выключен у профиля %s"
               % (options.profile or "активного")})
        return 2

    stop_flag = threading.Event()
    pause_flag = threading.Event()
    state = {"reader_dead": False}

    class _Control(API.Runner):
        """Runner с паузой: check() ждёт снятия паузы, но stop() побеждает."""

        def check(self):
            if self.stop_requested():
                raise API.MacroStopped("остановлено пользователем")
            while pause_flag.is_set() and not self.stop_requested():
                time.sleep(0.05)
            if self.stop_requested():
                raise API.MacroStopped("остановлено пользователем")
            if A.desktop_locked():
                raise API.MacroStopped("экран заблокирован — ввод не имеет смысла")

        def stop_requested(self):
            # Смерть родителя = остановка: сиротский макрос печатал бы в чужую
            # сессию, и остановить его было бы уже нечем.
            return stop_flag.is_set() or state["reader_dead"]

    try:
        given_args = json.loads(options.args or "{}")
    except ValueError:
        swallow.note('automator_runner.main:L103')
        given_args = {}

    last_line = {"value": None, "ts": 0.0}

    def log_line(text, level="info"):
        _emit({"event": "log", "text": str(text), "level": level})

    runner = _Control(log=log_line, dry=options.dry, profile=options.profile,
                      port=options.port, macro_path=options.macro,
                      macro_name=os.path.basename(options.macro),
                      args=given_args, stop_event=stop_flag)

    macro_path = os.path.abspath(options.macro)

    def tracer(frame, event, _arg):
        """Построчная проверка остановки + текущая строка для ленты.

        Трассируем только файл макроса: для остальных фреймов возвращаем None, и
        интерпретатор перестаёт их трассировать — иначе трассировка съедала бы
        время на каждой строчке stdlib.
        """
        if event == "call":
            here = os.path.abspath(frame.f_code.co_filename) == macro_path
            return tracer if here else None
        if event == "line":
            if runner.stop_requested():
                raise API.MacroStopped("остановлено пользователем")
            while pause_flag.is_set() and not runner.stop_requested():
                time.sleep(0.05)
            line = frame.f_lineno
            now = time.time()
            if line != last_line["value"] and now - last_line["ts"] > 0.05:
                last_line["value"], last_line["ts"] = line, now
                _emit({"event": "line", "line": line})
        return tracer

    def stdin_reader():
        """Команды родителя; EOF/закрытие = сервер умер."""
        try:
            stream = sys.stdin
            while True:
                raw = stream.readline()
                if not raw:
                    break
                try:
                    command = json.loads(raw)
                except ValueError:
                    swallow.note('automator_runner.stdin_reader:L151')
                    continue
                action = str(command.get("cmd") or "").lower()
                if action == "stop":
                    stop_flag.set()
                    _emit({"event": "status", "state": "stopping"})
                elif action == "pause":
                    pause_flag.set()
                    _emit({"event": "status", "state": "paused"})
                elif action == "resume":
                    pause_flag.clear()
                    _emit({"event": "status", "state": "running"})
        except Exception:
            swallow.note('automator_runner.stdin_reader:L164')
            pass
        finally:
            state["reader_dead"] = True

    reader = threading.Thread(target=stdin_reader, name="macro-stdin", daemon=True)
    reader.start()

    _emit({"event": "status", "state": "running", "dry": bool(options.dry)})

    # print() внутри макроса — это пользовательский вывод, а не протокол:
    # перехватываем stdout и отправляем его в stderr, иначе строка вида "готово"
    # прилетит в разбор JSON и прогон оборвётся на ровном месте.
    sys.stdout = sys.stderr
    source = io.open(macro_path, encoding="utf-8-sig").read()
    compiled = compile(source, macro_path, "exec")
    namespace = API.build_namespace(runner)
    namespace["__name__"] = "__macro__"
    namespace["__file__"] = macro_path

    code = 0
    reason = ""
    started = time.time()
    try:
        sys.settrace(tracer)
        exec(compiled, namespace, namespace)
    except API.MacroStopped as stopped:
        swallow.note('automator_runner.main:L191', stopped)
        reason = str(stopped) or "остановлено"
        code = 3
        # Прерывание посреди «ctrl + клики» оставляет модификатор зажатым:
        # отпускаем всегда, когда остановка не была штатным концом файла.
        if not options.dry:
            A.release_modifiers()
    except SystemExit:
        swallow.note('automator_runner.main:L199')
        reason = "макрос вызвал sys.exit()"
        code = 4
    except BaseException:
        reason = "исключение в макросе"
        code = 1
        _emit({"event": "error", "text": traceback.format_exc()})
        if not options.dry:
            A.release_modifiers()
    finally:
        sys.settrace(None)
        duration = time.time() - started
        _emit({"event": "result", "ok": code == 0, "code": code,
               "reason": reason, "seconds": round(duration, 3)})

    # Жёсткий страховочный край: если после stop что-то не даёт процессу выйти
    # (например, чужая блокировка в finally), родитель добьёт нас сам, но и здесь
    # не стоим бесконечно.
    if options.hard_kill and stop_flag.is_set():
        deadline = time.time() + max(0.2, float(options.hard_kill))
        while reader.is_alive() and time.time() < deadline:
            time.sleep(0.05)
    return code


if __name__ == "__main__":
    sys.exit(main())
