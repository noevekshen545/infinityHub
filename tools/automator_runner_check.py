"""Проверки процесс-раннера макросов: протокол, остановки, флаги, лента.

Запускает app/automator_runner.py как отдельный процесс (так же, как это будет
делать ядро модуля) и читает его stdout построчно. Всё в сухом режиме: ни один
event ввода не уходит в систему, поэтому проверкой можно заниматься за живым
рабочим столом.

Временный профиль zz-automator-check нужен одним-единственным причинам: флаг
модуля хранится в конфиге профиля, а тестить включённый флаг на admin — значит
оставить в его конфиге след. В конце скрипт удаляет каталог целиком, даже если
проверка упала.

    python tools/automator_runner_check.py
    python tools/automator_runner_check.py --python "E:\\...\\InfinitySearch.exe"
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

RUNNER = os.path.join(APP, "automator_runner.py")
SCRATCH_ID = "zz-automator-check"
OFF_PROFILE = "zz-automator-off"
TIMEOUT = 20.0

failures = []
checks = 0


def check(name, condition, detail=""):
    global checks
    checks += 1
    if not condition:
        failures.append("%s — %s" % (name, detail))
        print("  FAIL  %s  %s" % (name, detail))
    else:
        print("  ok    %s" % name)


def write(path, text):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _write_stdin(process, payload):
    try:
        process.stdin.write(payload)
        process.stdin.flush()
    except Exception:
        pass          # процесс уже умер — это увидим по коду выхода


def run_runner(interpreter, macro, profile, args=(), stdin_lines=(),
               close_stdin=False, delay_after_start=0.0, scripted=()):
    """Запуск раннера; возвращает (код выхода, события, stderr).

    `delay_after_start` и `scripted` отсчитываются от события status/running, а
    не от Popen: интерпретатор сборки стартует дольше, чем пауза, и команда легла
    бы в буфер stdin ещё до запуска макроса — проверка «остановили горячий цикл»
    тогда ловила бы мгновенную остановку, а не остановку в процессе работы.
    `scripted` — пары (секунды, команда), чтобы показать порядок паузы и
    возобновления, а не обе команды одновременно.
    """
    command = [interpreter, RUNNER, "--macro", macro, "--profile", profile,
               "--port", "0", "--dry"] + list(args)
    process = subprocess.Popen(command, cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace", bufsize=1)
    events = []
    timers = []
    payload = "".join(line + "\n" for line in stdin_lines)

    def schedule():
        if payload:
            timers.append(threading.Timer(delay_after_start,
                                          lambda: _write_stdin(process, payload)))
            timers[-1].start()
        for when, command in scripted:
            timers.append(threading.Timer(
                when, lambda line=json.dumps(command) + "\n": _write_stdin(process, line)))
            timers[-1].start()

    try:
        if payload and delay_after_start <= 0 and not scripted:
            _write_stdin(process, payload)
        if close_stdin:
            process.stdin.close()
        deadline = time.time() + TIMEOUT
        for raw in process.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                event = {"event": "!bad", "text": raw}
            events.append(event)
            if event.get("event") == "status" and event.get("state") == "running" \
                    and not timers:
                # Только первый запуск: resume тоже отдаёт state=running, и
                # повторная расстановка повторов перезапустила бы весь сценарий.
                schedule()
            if time.time() > deadline:
                break
        stderr = process.stderr.read()
        code = process.wait(timeout=5)
    finally:
        for timer in timers:
            timer.cancel()
        if process.poll() is None:
            process.kill()
            code = -1
            stderr = ""
    return code, events, stderr


def of(events, name):
    return [e for e in events if e.get("event") == name]


def bad_lines(events):
    return [e for e in events if e.get("event") == "!bad"]


def main():
    interpreter = sys.executable
    argv = sys.argv[1:]
    if "--python" in argv:
        interpreter = argv[argv.index("--python") + 1]

    scratch = os.path.join(ROOT, "data", "profiles", SCRATCH_ID)
    macro_dir = os.path.join(scratch, "data", "automator")
    print("интерпретатор: %s" % interpreter)
    print("лента шагов")
    import automator_lane as L

    source = ('k.press("ctrl+alt+f4")\n'
              'x = 1\n'
              'if w.exists("Блокнот"):\n'
              '    m.click(10, -20)\n'
              'for i in range(2):\n'
              '    k.text("раз")\n'
              'm.wheel(-2)\n'
              'sleep(0.01)\n')
    parsed = L.lane(source)
    check("лента разбирает вложенные блоки", parsed["ok"] and parsed["count"] == 8,
          "получено %s шагов, ошибка %r" % (parsed.get("count"), parsed.get("error")))
    labels = " | ".join(step["label"] for step in parsed["steps"])
    check("подписи на русском", "клавиши: ctrl+alt+f4" in labels and "если: w.exists" in labels,
          labels)
    check("отрицательные координаты в подписи", "в 10,-20" in labels and "колесо: -2" in labels, labels)
    syntax = L.lane('k.press(')
    check("синтаксис-ошибка одной строкой", not syntax["ok"] and syntax["error"].startswith("строка 1"),
          repr(syntax["error"]))

    print("раннер: сухой прогон")
    write(os.path.join(scratch, "config.json"),
          json.dumps({"modules": {"automator": {"enabled": True}}}, ensure_ascii=False))
    happy = os.path.join(macro_dir, "happy.py")
    write(happy, source + 'print("вывод макроса")\nui.step("готово")\n')
    code, events, stderr = run_runner(interpreter, happy, SCRATCH_ID)
    check("код выхода 0", code == 0, "код %s, stderr %s" % (code, stderr[-300:]))
    check("нет мусора в протоколе", not bad_lines(events), str(bad_lines(events)[:2]))
    check("статус running", [e.get("state") for e in of(events, "status")] == ["running"],
          str(of(events, "status")))
    result = of(events, "result")
    check("итог есть и он успешный", len(result) == 1 and result[0]["ok"], str(result))
    logs = [e["text"] for e in of(events, "log")]
    check("журнал шагов дошёл", "готово" in "".join(logs) and len(logs) >= 6, str(logs))
    check("print макроса не стал протоколом", "вывод макроса" not in logs, str(logs))

    print("раннер: трассировка строк")
    paced = os.path.join(macro_dir, "paced.py")
    write(paced, 'k.press("f4")\nsleep(0.12)\nk.press("f5")\nsleep(0.12)\n'
                 'k.press("f6")\nsleep(0.12)\nk.press("f7")\n')
    code, events, stderr = run_runner(interpreter, paced, SCRATCH_ID)
    lines = [e["line"] for e in of(events, "line")]
    check("лента видела бы несколько строк", code == 0 and len(lines) >= 3, str(lines))
    check("строки идут по возрастанию", lines == sorted(lines), str(lines))

    print("раннер: остановка по команде")
    loopy = os.path.join(macro_dir, "loopy.py")
    write(loopy, "import time\n"
                 "deadline = time.time() + 30\n"
                 "while time.time() < deadline:\n"
                 "    pass\n")
    code, events, stderr = run_runner(interpreter, loopy, SCRATCH_ID,
                                      stdin_lines=[json.dumps({"cmd": "stop"})],
                                      delay_after_start=0.4)
    result = of(events, "result")
    check("стоп прервал бесконечный цикл", code == 3 and result and result[0]["reason"],
          "код %s, %s" % (code, str(result)[:200]))
    check("трассировка не залила поток родителя", len(of(events, "line")) < 50,
          "%d строка(ов) за горячий цикл" % len(of(events, "line")))
    check("цикл жил, а не умер мгновенно",
          result and result[0]["seconds"] >= 0.3, str(result)[:200])

    print("раннер: пауза и возобновление")
    paced2 = os.path.join(macro_dir, "paced2.py")
    write(paced2, "for i in range(6):\n    sleep(0.1)\n    ui.step('шаг %d' % i)\n")
    code, events, stderr = run_runner(
        interpreter, paced2, SCRATCH_ID,
        scripted=[(0.25, {"cmd": "pause"}), (0.85, {"cmd": "resume"})])
    states = [e.get("state") for e in of(events, "status")]
    result = of(events, "result")
    check("пауза и продолжение подтверждены",
          code == 0 and states[:3] == ["running", "paused", "running"], str(states))
    check("пауза действительно держала макрос",
          result and result[0]["seconds"] >= 1.0, str(result)[:200])

    print("раннер: смерть родителя")
    code, events, stderr = run_runner(interpreter, loopy, SCRATCH_ID, close_stdin=True)
    result = of(events, "result")
    check("сиротский макрос остановился", code == 3 and result,
          "код %s, %s" % (code, str(result)[:200]))

    print("раннер: исключение в макросе")
    boom = os.path.join(macro_dir, "boom.py")
    write(boom, 'k.press("ctrl+alt+f4")\nraise ValueError("сломано намеренно")\n')
    code, events, stderr = run_runner(interpreter, boom, SCRATCH_ID)
    errors = of(events, "error")
    check("код 1 и трейсбек", code == 1 and errors and "сломано намеренно" in errors[0]["text"],
          "код %s, %s" % (code, str(errors)[:200]))
    check("шаг до падения записан", any("ctrl+alt+f4" in e["text"] for e in of(events, "log")),
          str([e["text"] for e in of(events, "log")]))

    print("флаг модуля")
    off_dir = os.path.join(ROOT, "data", "profiles", OFF_PROFILE)
    os.makedirs(off_dir, exist_ok=True)
    dead = os.path.join(macro_dir, "dead.py")
    write(dead, 'ui.step("не должно исполниться")\n')
    code, events, stderr = run_runner(interpreter, dead, OFF_PROFILE)
    check("выключенный флаг: код 2 и ничего не введено",
          code == 2 and of(events, "error") and not of(events, "log"),
          "код %s, %s" % (code, str(events)[:200]))
    shutil.rmtree(off_dir, ignore_errors=True)

    print("исполнение файла вне библиотеки")
    outside = os.path.join(ROOT, "data", "profiles", SCRATCH_ID, "outside.py")
    write(outside, 'ui.step("снаружи")\n')
    code, events, _ = run_runner(interpreter, outside, SCRATCH_ID)
    check("макрос из произвольного пути исполняется", code == 0, "код %s" % code)

    print("\n%d проверок, %s провалов" % (checks, len(failures)))
    for failure in failures:
        print("  ПРОВАЛ: %s" % failure)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(os.path.join(ROOT, "data", "profiles", SCRATCH_ID), ignore_errors=True)
