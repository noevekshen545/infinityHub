#!/usr/bin/env python
"""Наблюдатель за «чат ии.md» — файлом переговоров двух ИИ.

Зачем: второй агент пишет в тот же файл, и его сообщения нужно замечать,
не теряя ни одной правки. Хранит снимки каждой версии файла и печатает
дельту относительно последнего просмотренного мной состояния.

  python tools/ai_chat_watch.py --once          # что изменилось с прошлого просмотра
  python tools/ai_chat_watch.py --wait [sec]    # спать до изменения, вывести дельту, exit 0
                                                  # (по истечении таймаута — exit 2)
  python tools/ai_chat_watch.py --history       # список снимков
  python tools/ai_chat_watch.py --restore N     # вернуть снимок №N (защита от перезаписи:
                                                  # Write перетирает файл целиком)

Снимки и аудит — в data/chat_ii_history/ (создаётся сам).
"""
import argparse
import datetime as dt
import difflib
import hashlib
import os
import shutil
import sys
import time

CHAT = "чат ии.md"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "data", "chat_ii_history")
STATE = os.path.join(HIST, "seen.json")


def _path():
    return os.path.join(ROOT, CHAT)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _now():
    return dt.datetime.now().strftime("%m-%d %H:%M:%S")


def _read():
    try:
        with open(_path(), encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""


def _load_state():
    try:
        import json
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"digest": "", "shots": 0}


def _save_state(st):
    os.makedirs(HIST, exist_ok=True)
    import json
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def _snapshot(text, tag):
    """Копия каждой непохожей версии файла: перетёр один агент другого —
    всегда есть чем восстановить."""
    os.makedirs(HIST, exist_ok=True)
    st = _load_state()
    n = st.get("shots", 0) + 1
    name = "%03d_%s_%s.md" % (n, tag, dt.datetime.now().strftime("%H%M%S"))
    with open(os.path.join(HIST, name), "w", encoding="utf-8") as f:
        f.write(text)
    if n > 400:
        old = sorted(x for x in os.listdir(HIST) if x.endswith(".md"))[:60]
        for x in old:
            try:
                os.remove(os.path.join(HIST, x))
            except OSError:
                pass
    st["shots"] = n
    return st, name


def _audit(line):
    os.makedirs(HIST, exist_ok=True)
    log = os.path.join(HIST, "audit.log")
    prev = []
    try:
        with open(log, encoding="utf-8") as f:
            prev = f.read().splitlines()
    except OSError:
        pass
    with open(log, "w", encoding="utf-8") as f:
        f.write("\n".join(prev[-3999:] + [line]) + "\n")


def cmd_seed():
    """Зафиксировать текущее содержимое как «я это уже видел».

    Нужно перед тем, как слушать: собственные правки файла тоже считаются
    изменением, и наблюдатель иначе будит сам себя.
    """
    text = _read()
    st, name = _snapshot(text, "seed")
    st["digest"] = _digest(text)
    st["last_text"] = text
    _save_state(st)
    print("база обновлена: %s (%d B)" % (name, len(text.encode("utf-8"))))
    return 0


def diff_of(old, new):
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                      fromfile="видел", tofile="сейчас", lineterm=""))
    return "\n".join(lines)


def cmd_once(mark_seen=True):
    text = _read()
    st = _load_state()
    dig = _digest(text)
    if dig == st.get("digest"):
        print("изменений нет (%d B, %s)" % (len(text.encode("utf-8")), _now()))
        return 1
    old = st.get("last_text", "")
    d = diff_of(old, text)
    st, name = _snapshot(text, "new")
    if mark_seen:
        st["digest"] = dig
        st["last_text"] = text
    _save_state(st)
    _audit("%s изменение: %+d B, снимок %s" % (_now(), len(text) - len(old), name))
    print("ЕСТЬ ИЗМЕНЕНИЯ (снимок %s):" % name)
    print(d if d else "(изменился объём/порядок, диф пуст)")
    return 0


def cmd_wait(timeout):
    st = _load_state()
    base = st.get("digest") or _digest(_read())
    if not st.get("digest"):
        rc = cmd_seed()
        st = _load_state()
        base = st["digest"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        text = _read()
        if _digest(text) != base:
            return cmd_once(mark_seen=True)
    print("тишина %d с: вторая ИИ ничего не писала" % timeout)
    return 2


def cmd_history():
    if not os.path.isdir(HIST):
        print("снимков нет")
        return 0
    for x in sorted(os.listdir(HIST)):
        if x.endswith(".md"):
            p = os.path.join(HIST, x)
            print(x, os.path.getsize(p), "B", time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))))
    return 0


def cmd_restore(num):
    if not os.path.isdir(HIST):
        print("снимков нет"); return 1
    cands = [x for x in sorted(os.listdir(HIST)) if x.startswith("%03d_" % num)]
    if not cands:
        print("снимок %d не найден" % num); return 1
    src = os.path.join(HIST, cands[0])
    shutil.copy2(_path(), os.path.join(HIST, "before_restore_%s.md" % dt.datetime.now().strftime("%H%M%S")))
    shutil.copy2(src, _path())
    print("восстановлен %s" % cands[0])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--wait", type=int, const=900, nargs="?", metavar="SEC")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--seed", action="store_true", help="зафиксировать текущее как виденное")
    ap.add_argument("--restore", type=int, metavar="N")
    a = ap.parse_args()
    if a.seed:
        sys.exit(cmd_seed())
    if a.restore is not None:
        sys.exit(cmd_restore(a.restore))
    if a.history:
        sys.exit(cmd_history())
    if a.wait is not None:
        sys.exit(cmd_wait(a.wait))
    sys.exit(cmd_once())


if __name__ == "__main__":
    main()
