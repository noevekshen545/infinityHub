"""Проглоченное исключение = ошибка, которой как бы не существует.

Молчаливый `except: pass` стоил этому приложению уже не один день расследований:
`database is locked`, потерянные треки и пустые страницы выглядели как «всё в
порядке», потому что обработчик не оставлял ни кода ошибки, ни строки в логе.

`note()` вызывается из каждого такого места и ничего не меняет в потоке
управления — только пишет след. Первое за процесс вхождение точки даёт WARNING с
типом и текстом ошибки, каждое следующее — DEBUG с номером вхождения. Так петля
по тысяче файлов не заливает лог, но первая же ошибка каждой точки видна
сразу и ведёт ровно к тому месту, где она проглочена.

Вызывается так: `swallow.note("media_module.get_music_index:L4521")` — ошибку
модуль берёт из `sys.exc_info()`, поэтому обработчик не обязан иметь привязку
`as e` и может быть вообще `except:`.
"""

import logging
import sys
import threading

# Дочерний логгер «infinity»: у него handler'ы навешаны на «infinity» в
# legacy/rotating_logging.py, а propagate у того выключен, поэтому отдельный
# корневой логгер писал бы в пустоту. Так следы попадают в тот же server.log с
# ротацией и в консоль — туда же, куда и остальные строки сервера.
_log = logging.getLogger("infinity.swallow")
_lock = threading.Lock()
_seen = {}


def note(where, err=None):
    """Записать проглоченное исключение. Возвращает номер вхождения этой точки.

    Логирование не имеет право ронять выдачу, поэтому любые свои сбои `note()`
    глотает сам — но уже в stderr, чтобы не остаться совсем невидимым.
    """
    try:
        if err is None:
            err = sys.exc_info()[1]
        with _lock:
            n = _seen.get(where, 0) + 1
            _seen[where] = n
        text = type(err).__name__ if err is not None else "exception"
        detail = f"{text}: {err}" if err is not None else text
        if n == 1:
            _log.warning("%s: исключение проглочено (%s)", where, detail)
        else:
            _log.debug("%s: проглочено снова (#%d): %s", where, n, detail)
        return n
    except BaseException as exc:  # noqa: BLE001 - логгер не должен ломать запрос
        try:
            sys.stderr.write(f"swallow.note({where!r}) не смог записать: {exc}\n")
        except BaseException:
            # Оба канала живы? нет: stderr не дал строку — пробуем логгер (это
            # другой обработчик, файл лога может писаться, когда консоль закрыта).
            try:
                _log.warning("%s: след не удалось записать в stderr (%s)", where, exc)
            except BaseException:
                # Дальше сообщать нечем: и stderr, и логгер недоступны. Рекурсии
                # быть не должно — это дно, выход только вверх по стеку.
                pass
        return 0


def stats():
    """Счётчики по точкам: сколько раз что молча провалилось с начала процесса."""
    with _lock:
        return dict(_seen)


def reset():
    with _lock:
        _seen.clear()
