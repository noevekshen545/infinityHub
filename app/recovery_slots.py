"""Recovery Guard 3.0 — A/B-слоты кода (guard.md §4).

Слот — это ПОЛНАЯ копия файлов кода приложения в data/recovery_slot_{a,b}/.
Активный слот выбирается в recovery_state.json (ключ "_active_slot"); гвард
запускает main.py из активного слота. Пока слот не выбран — работает обычный
каталог APP_DIR (режим без слотов, обратная совместимость).

Восстановление из снимка = распаковка файлов снимка в НЕактивный слот
(с проверкой компиляции каждой .py), затем автокенарий-загрузка, затем
переключение указателя. Никогда не пишем в живой APP_DIR.

Только stdlib — модуль используется и внутри FastAPI, и в тестах.
"""

import json
import os
import shutil
import stat as stat_mod
import subprocess
import sys
import time

import recovery_core as rc
import swallow

SLOTS_DIR_NAME = "recovery_slot_%s"      # recovery_slot_a / recovery_slot_b
QUARANTINE_DIRNAME = "recovery_quarantine"
STATE_KEY = "_active_slot"
SLOT_NAMES = ("a", "b")
# Директории, которые НЕ джанкодим из живого APP_DIR в слот (это либо
# сами слоты/бэкапы, либо кэш, который не нужен для загрузки).
_LINK_EXCLUDE = {
    "__pycache__", "recovery_slot_a", "recovery_slot_b",
    "recovery_backups", "recovery_quarantine", "recovery_tmp",
    "recovery_state.json", "recovery_log.txt",
}


def _slots_dir() -> str:
    return rc.DATA_DIR


def slot_dir(slot: str) -> str:
    """Абсолютный путь слота ('a'|'b')."""
    slot = str(slot).strip().lower()
    if slot not in SLOT_NAMES:
        raise ValueError("слот только 'a' или 'b'")
    return os.path.join(_slots_dir(), SLOTS_DIR_NAME % slot)


def quarantine_dir() -> str:
    return os.path.join(_slots_dir(), QUARANTINE_DIRNAME)


# --------------------------------------------------------------------- state -

def _state_path() -> str:
    return os.path.join(rc.DATA_DIR, "recovery_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        swallow.note('recovery_slots._load_state:L66')
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def active_slot() -> str:
    """'a' | 'b' | '' (слоты не используются)."""
    name = str(_load_state().get(STATE_KEY, "")).strip().lower()
    return name if name in SLOT_NAMES else ""


def set_active_slot(slot: str) -> None:
    slot = str(slot).strip().lower()
    if slot not in SLOT_NAMES:
        raise ValueError("слот только 'a' или 'b'")
    state = _load_state()
    state[STATE_KEY] = slot
    _save_state(state)


def inactive_slot() -> str:
    """Слот, в который будем писать при восстановлении."""
    cur = active_slot()
    if cur == "a":
        return "b"
    if cur == "b":
        return "a"
    return "a"


def launch_dir() -> str:
    """Каталог, из которого гвард должен запустить main.py."""
    cur = active_slot()
    if cur:
        d = slot_dir(cur)
        if os.path.isfile(os.path.join(d, "main.py")):
            return d
    return rc.APP_DIR


# ------------------------------------------------------- junction / rmtree --

def _is_reparse_dir(path: str) -> bool:
    """Каталог-джанкод/симлинг (Windows reparse point)."""
    if os.path.islink(path):
        return True
    try:
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        swallow.note('recovery_slots._is_reparse_dir:L125')
        return False


def _junction(src: str, dst: str) -> str:
    """Джанкод src->dst (без прав админа). Возвращает способ."""
    if os.path.exists(dst):
        return "exists"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # 1) _winapi.CreateJunction — надёжно и без cmd
    try:
        import _winapi
        _winapi.CreateJunction(src, dst)
        return "junction"
    except Exception:
        swallow.note('recovery_slots._junction:L140')
        pass
    # 2) mklink /J
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                           capture_output=True, timeout=15)
        if r.returncode == 0:
            return "junction"
    except Exception:
        swallow.note('recovery_slots._junction:L149')
        pass
    # 3) симлинк (может требовать Developer Mode)
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    except Exception:
        swallow.note('recovery_slots._junction:L156')
        pass
    # 4) fallback — обычная копия (медленно, но работает)
    shutil.copytree(src, dst, symlinks=True)
    return "copy"


def _rmtree_safe(path: str) -> None:
    """rmtree, который НЕ заходит внутрь джанкодов (иначе сотрёт цель)."""
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and _is_reparse_dir(path):
        os.rmdir(path)          # снимает саму точку, цель цела
        return

    def _force_rw(fn, p, excinfo):
        try:
            os.chmod(p, stat_mod.S_IWRITE)
            fn(p)
        except Exception:
            swallow.note('recovery_slots._force_rw:L176')
            pass

    def _walk(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p) and not os.path.islink(p):
                if _is_reparse_dir(p):
                    os.rmdir(p)
                else:
                    _walk(p)
                    os.rmdir(p)
            else:
                try:
                    os.remove(p)
                except PermissionError:
                    swallow.note('recovery_slots._walk:L192')
                    _force_rw(os.remove, p, None)

    _walk(path)
    shutil.rmtree(path, onerror=_force_rw)


def _purge_pycache(root: str) -> int:
    """Удалить все __pycache__ под root (§4.2 — против «мёртвых» pyc).

    Обход через scandir: в джанкоды НЕ спускаемся (иначе вычистили бы
    __pycache__ живого APP_DIR — наша цель не трогать).
    """
    removed = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            swallow.note('recovery_slots._purge_pycache:L212')
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            if _is_reparse_dir(e.path):
                continue
            if e.name == "__pycache__":
                try:
                    _rmtree_safe(e.path)
                    removed += 1
                except Exception:
                    swallow.note('recovery_slots._purge_pycache:L224')
                    pass
            else:
                stack.append(e.path)
    return removed


# ------------------------------------------------------------ сборка слота --

def _copy_code_file(src: str, dst: str) -> tuple:
    """Скопировать один файл кода. .py — только после проверки компиляции.

    Возвращает (ok, reason). При сломанной .py файл НЕ попадает в слот
    (§4.2: битый файл не должен уронить ни канарейку, ни боевой запуск).
    """
    if src.lower().endswith(".py"):
        try:
            with open(src, "rb") as f:
                src_code = f.read()
            compile(src_code, src, "exec")
        except SyntaxError as e:
            swallow.note('recovery_slots._copy_code_file:L245', e)
            return False, f"syntax: {e.msg} (line {e.lineno})"
        except Exception as e:
            swallow.note('recovery_slots._copy_code_file:L248', e)
            return False, f"read: {e}"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True, ""


def _link_missing_dirs(src_root: str, dst_root: str) -> dict:
    """Рекурсивно джанкодом подтянуть из живого APP_DIR каталоги, которых
    нет в слоте (templates/static/binaries/... на ЛЮБОЙ глубине).

    Существующие в слоте каталоги не трогаем (там лежат реальные копии
    кода из снимка) — спускаемся только в них.
    """
    linked = {}
    stack = [(src_root, dst_root)]
    while stack:
        src, dst = stack.pop()
        try:
            entries = list(os.scandir(src))
        except OSError:
            swallow.note('recovery_slots._link_missing_dirs:L269')
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            name = e.name
            if _is_reparse_dir(e.path):
                continue                       # уже ссылка — не копируем
            if name == "__pycache__" or name in _LINK_EXCLUDE:
                continue
            # не джанкодим каталог, внутри которого лежат слоты
            # (легаси: INFINITY_DATA_DIR=app/data → слот внутри app/data)
            sd = _slots_dir()
            if sd == e.path or sd.startswith(e.path + os.sep):
                continue
            d_path = os.path.join(dst, name)
            if os.path.lexists(d_path):
                stack.append((e.path, d_path))  # есть в слоте — проверяем глубже
                continue
            try:
                linked[name] = _junction(e.path, d_path)
            except Exception as ex:
                swallow.note('recovery_slots._link_missing_dirs:L291', ex)
                linked[name] = f"error: {ex}"
    return linked


def build_slot(slot: str, files: list = None) -> dict:
    """Собрать слот: очистить → скопировать файлы кода → джанкоды.

    files — список абсолютных путей (по умолчанию — discover_code_files()
    живого APP_DIR). .py проверяются compile(); битые пропускаются.
    """
    dst_root = slot_dir(slot)
    _rmtree_safe(dst_root)
    os.makedirs(dst_root, exist_ok=True)

    if files is None:
        files = rc.discover_code_files()
    copied, skipped = 0, []
    for item in files:
        # discover_code_files отдаёт кортежи (src, rel, size)
        if isinstance(item, (tuple, list)):
            src, rel = item[0], item[1]
        else:
            src = item
            rel = os.path.relpath(src, rc.APP_DIR)
        if rel.startswith("..") or os.path.isabs(rel):
            skipped.append({"file": str(rel), "reason": "вне APP_DIR"})
            continue
        ok, reason = _copy_code_file(src, os.path.join(dst_root, rel))
        if ok:
            copied += 1
        else:
            skipped.append({"file": rel, "reason": reason})

    linked = _link_missing_dirs(rc.APP_DIR, dst_root)
    purged = _purge_pycache(dst_root)
    return {"slot": slot, "dir": dst_root, "copied": copied,
            "skipped": skipped, "linked": linked, "pycache_purged": purged}


# --------------------------------------------------- канарейка и quarantine --

def _canary_boot(app_dir: str, timeout=None, port=None) -> dict:
    """Smoke-boot ИМЕННО из каталога (слота), как verify_l3, но без привязки
    к структуре снимка: app_dir должен содержать main.py."""
    import urllib.error
    import urllib.request
    cfg = rc.load_config()
    port = int(port or cfg.get("canary_port", 18099))
    timeout = int(timeout or cfg.get("canary_timeout_sec", 180))
    if not os.path.isfile(os.path.join(app_dir, "main.py")):
        return {"ok": False, "errors": ["main.py not found"], "error_count": 1}
    env = dict(os.environ)
    env.update({"INFINITY_APP_DIR": app_dir, "INFINITY_ROOT": rc.ROOT_DIR,
                "INFINITY_DATA_DIR": rc.DATA_DIR, "INFINITY_PORT": str(port),
                "INFINITY_SAFE_MODE": "1", "INFINITY_SNAPSHOT_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        # См. verify_l3: без "--server" канарейка норовит подняться на порту
        # из config.json через собственный guard, а не на канареечном.
        proc = subprocess.Popen(
            [sys.executable, os.path.join(app_dir, "main.py"), "--server",
             "--port", str(port), "--host", "127.0.0.1"],
            cwd=app_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
    except Exception as e:
        swallow.note('recovery_slots._canary_boot:L358', e)
        return {"ok": False, "errors": [f"spawn failed: {e}"],
                "error_count": 1}
    ok = False
    tail = []
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                    if r.status == 200:
                        ok = True
                        break
            except (urllib.error.URLError, OSError, ValueError):
                swallow.note('recovery_slots._canary_boot:L375')
                time.sleep(2)
    finally:
        rc._terminate(proc)
        try:
            tail = (proc.stdout.read() or "").splitlines()[-25:]
        except Exception:
            swallow.note('recovery_slots._canary_boot:L382')
            pass
    errors = [] if ok else ["health check never returned 200"]
    for ln in tail:
        low = ln.lower()
        if "traceback" in low or "error" in low or "exception" in low:
            errors.append(ln.strip()[:200])
    return {"ok": ok, "port": port, "errors": errors[:25],
            "error_count": len(errors), "log_tail": tail[-15:]}


def quarantine_current() -> dict:
    """§4.2.1: текущую версию кода → data/recovery_quarantine/<ts>/.

    Это ВОССТАНОВИТЕЛЬНАЯ копия: откат из сломанного состояния всегда
    обратим. Храним максимум 5 последних копий.
    """
    src_root = launch_dir()
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(quarantine_dir(), ts)
    if os.path.lexists(dst):
        dst += f"-{os.getpid()}"
    os.makedirs(dst, exist_ok=True)
    copied = 0
    for item in rc.discover_code_files(src_root):
        # discover_code_files отдаёт кортежи (src, rel, size)
        if isinstance(item, (tuple, list)):
            src, rel = item[0], item[1]
        else:
            src = item
            rel = os.path.relpath(src, src_root)
        ok, _r = _copy_code_file(src, os.path.join(dst, rel))
        if ok:
            copied += 1
    # ротация: оставляем 5 последних
    try:
        entries = sorted(os.listdir(quarantine_dir()))
        for name in entries[:-5]:
            _rmtree_safe(os.path.join(quarantine_dir(), name))
    except Exception:
        swallow.note('recovery_slots.quarantine_current:L422')
        pass
    return {"dir": dst, "files": copied}


# ------------------------------------------------------- restore / activate --

def restore_to_slot(snap_id: str, slot: str = None, canary: bool = True,
                    port=None, force: bool = False) -> dict:
    """§4.2: восстановление КОДА из снимка в неактивный слот.

    1) проверка целостности снимка;
    2) quarantine текущей версии;
    3) распаковка файлов снимка в слот (битые .py пропускаются);
    4) автокенарий-загрузка из слота;
    5) переключение указателя (при canary=False — сразу).

    Живой APP_DIR не изменяется. При провале канарейки активация не
    происходит — активной остаётся прежняя версия.
    """
    ok, detail = rc.check_integrity(snap_id)
    if not ok:
        return {"ok": False, "stage": "integrity",
                "error": f"снимок повреждён: {detail}"}
    snap_dir = rc.snapshot_path(snap_id)
    man = rc.read_manifest(snap_dir)
    files = man.get("files") or []
    if not files:
        return {"ok": False, "stage": "manifest",
                "error": "в снимке нет файлов"}

    target = (slot or inactive_slot()).strip().lower()
    if target not in SLOT_NAMES:
        return {"ok": False, "stage": "args", "error": "слот только 'a'/'b'"}

    # §4.4: анти-цикл «откат → снова откат» — lock требует ручного
    # подтверждения (POST .../recovery-mode/unlock), force его обходит.
    lock = restore_lock_status()
    if lock.get("locked") and not force:
        return {"ok": False, "stage": "lock",
                "error": "restore_lock: требуется ручное подтверждение",
                "lock": lock}

    try:
        quarantine = quarantine_current()
    except Exception as e:
        swallow.note('recovery_slots.restore_to_slot:L468', e)
        return {"ok": False, "stage": "quarantine", "error": str(e)}

    # распаковка в слот (слот предварительно очищается — «призраков» нет)
    dst_root = slot_dir(target)
    _rmtree_safe(dst_root)
    os.makedirs(dst_root, exist_ok=True)
    restored, skipped = 0, []
    for item in files:
        rel = item.get("rel") or ""
        stored = item.get("stored") or ""
        if not rel or not stored:
            continue
        src = os.path.join(snap_dir, stored)
        if not os.path.isfile(src):
            skipped.append({"file": rel, "reason": "нет в снимке"})
            continue
        c_ok, reason = _copy_code_file(src, os.path.join(dst_root, rel))
        if c_ok:
            restored += 1
        else:
            skipped.append({"file": rel, "reason": reason})
    linked = _link_missing_dirs(rc.APP_DIR, dst_root)
    purged = _purge_pycache(dst_root)

    report = {"target_slot": target, "restored": restored,
              "skipped": skipped, "linked": linked,
              "pycache_purged": purged, "quarantine": quarantine,
              "id": snap_id}

    if canary:
        car = _canary_boot(dst_root, port=port)
        report["canary"] = car
        if not car.get("ok"):
            report.update(ok=False, stage="canary",
                          error="канарейка провалена — активация не выполнена")
            # §4.4: снимок не прошёл L3 → restore_lock сразу
            report["lock"] = record_restore_failure("canary failed", hard=True)
            rc.prune_log(f"restore {snap_id} -> slot {target}: canary FAILED")
            return report

    set_active_slot(target)
    restore_attempts_reset()   # успешный откат обнуляет серию «подряд»
    report["ok"] = True
    report["active"] = target
    rc.prune_log(f"restore {snap_id} -> slot {target}: activated"
                 f" (canary={'on' if canary else 'off'})")
    return report


def activate_slot(slot: str) -> dict:
    """§4: откат / «откат отката» — быстрое переключение без распаковки."""
    slot = str(slot).strip().lower()
    if slot not in SLOT_NAMES:
        return {"ok": False, "error": "слот только 'a'/'b'"}
    d = slot_dir(slot)
    if not os.path.isfile(os.path.join(d, "main.py")):
        return {"ok": False, "error": f"слот {slot} пуст или не собран"}
    set_active_slot(slot)
    rc.prune_log(f"slot {slot} activated manually")
    return {"ok": True, "active": slot, "dir": d}


def _dir_stats(path: str) -> dict:
    """Размер/число файлов каталога слота — без спуска в джанкоды."""
    files, total, has_main = 0, 0, False
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            swallow.note('recovery_slots._dir_stats:L540')
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if _is_reparse_dir(e.path):
                    continue          # джанкод на живые данные не считаем
                if e.name != "__pycache__":
                    stack.append(e.path)
            elif e.is_file(follow_symlinks=False):
                files += 1
                if e.name == "main.py" and d == path:
                    has_main = True
                try:
                    total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    swallow.note('recovery_slots._dir_stats:L555')
                    pass
    return {"files": files, "size_mb": round(total / 1048576, 2),
            "has_main": has_main}


def slots_status() -> dict:
    """Статус для GET /api/admin/recovery/slots."""
    active = active_slot()
    slots = {}
    for name in SLOT_NAMES:
        d = slot_dir(name)
        info = {"exists": os.path.isdir(d), "dir": d}
        if info["exists"]:
            info.update(_dir_stats(d))
            info["active"] = (name == active)
        slots[name] = info
    try:
        quarantined = sorted(os.listdir(quarantine_dir()))
    except Exception:
        swallow.note('recovery_slots.slots_status:L575')
        quarantined = []
    return {"active": active or None,
            "launch_dir": launch_dir(),
            "slots": slots,
            "quarantine": quarantined}


# -------------------------------------------- режим восстановления (§4.1) --

RECOVERY_MODE_KEY = "recovery_mode"     # {active, snapshot, reason, ...}
RESTORE_LOCK_KEY = "restore_lock"       # §4.4: ручное подтверждение
RESTORE_ATTEMPTS_KEY = "restore_attempts"
RESTORE_ATTEMPTS_MAX = 3


def recovery_mode_get() -> dict:
    """Персистентный режим восстановления; {\"active\": False} — выключен."""
    mode = _load_state().get(RECOVERY_MODE_KEY)
    if isinstance(mode, dict) and mode.get("active"):
        return mode
    return {"active": False}


def recovery_mode_set(snapshot_id: str, reason: str = "manual") -> dict:
    """Включить режим: каждый запуск гварда применяет снимок (§4.1).

    Снимок должен пройти проверку целостности — иначе режим не ставим.
    """
    ok, detail = rc.check_integrity(snapshot_id)
    if not ok:
        return {"ok": False, "stage": "integrity",
                "error": f"снимок повреждён: {detail}"}
    state = _load_state()
    state[RECOVERY_MODE_KEY] = {
        "active": True, "snapshot": snapshot_id, "reason": str(reason),
        "ts": int(time.time()), "slot": inactive_slot(),
        "applied_slot": None, "last_apply": 0,
    }
    _save_state(state)
    rc.prune_log(f"recovery mode ON: {snapshot_id} ({reason})")
    return {"ok": True, "mode": state[RECOVERY_MODE_KEY]}


def recovery_mode_clear() -> dict:
    """Снять режим — только кнопкой (проверка boot_ok — на стороне API)."""
    state = _load_state()
    mode = state.pop(RECOVERY_MODE_KEY, None)
    state.pop("auto_restore_suggested", None)
    _save_state(state)
    rc.prune_log(f"recovery mode OFF (was: "
                 f"{(mode or {}).get('snapshot', '-')})")
    return {"ok": True, "was": mode or {"active": False}}


def recovery_mode_apply(reason: str = "guard_start") -> dict:
    """Применить режим: заново убедиться, что активный слот = снимок режима.

    Вызывается гвардом на КАЖДОМ запуске. Если слот уже содержит снимок
    режима — быстрый выход без распаковки.
    """
    mode = recovery_mode_get()
    if not mode.get("active"):
        return {"applied": False, "skipped": "mode_off"}
    lock = restore_lock_status()
    if lock.get("locked"):
        return {"applied": False, "skipped": "restore_lock", "lock": lock}
    snap = str(mode.get("snapshot") or "")
    if not snap:
        return {"applied": False, "skipped": "no_snapshot"}
    act = active_slot()
    if act and mode.get("applied_slot") == act and \
            os.path.isfile(os.path.join(slot_dir(act), "main.py")):
        return {"applied": False, "skipped": "already_active", "slot": act}
    report = restore_to_slot(snap, canary=False)   # канарейка была при установке
    if report.get("ok"):
        state = _load_state()
        m = state.get(RECOVERY_MODE_KEY) or {}
        m.update({"applied_slot": report.get("target_slot"),
                  "last_apply": int(time.time()), "apply_reason": reason})
        state[RECOVERY_MODE_KEY] = m
        _save_state(state)
        report["applied"] = True
        rc.prune_log(f"recovery mode applied: {snap} -> "
                     f"{report.get('target_slot')} ({reason})")
    return report


# --------------------------------------------- restore_lock (§4.4 анти-цикл) --

def restore_lock_status() -> dict:
    st = _load_state()
    lock = st.get(RESTORE_LOCK_KEY)
    lock = lock if isinstance(lock, dict) else {}
    return {"locked": bool(lock.get("active")),
            "attempts": int(st.get(RESTORE_ATTEMPTS_KEY, 0) or 0),
            "threshold": RESTORE_ATTEMPTS_MAX,
            "reason": lock.get("reason", ""),
            "ts": lock.get("ts", 0)}


def record_restore_failure(reason: str = "restore",
                           hard: bool = False) -> dict:
    """Откат не удался. hard=True — «снимок не прошёл L3» → lock сразу;
    иначе lock ставится после RESTORE_ATTEMPTS_MAX попыток подряд."""
    st = _load_state()
    attempts = int(st.get(RESTORE_ATTEMPTS_KEY, 0) or 0) + 1
    st[RESTORE_ATTEMPTS_KEY] = attempts
    if hard or attempts >= RESTORE_ATTEMPTS_MAX:
        st[RESTORE_LOCK_KEY] = {"active": True, "reason": reason,
                                "ts": int(time.time()), "attempts": attempts}
        rc.prune_log(f"restore_lock ON: {reason} (attempts={attempts})")
    _save_state(st)
    return restore_lock_status()


def restore_attempts_reset() -> dict:
    """Успешный старт/откат обнуляет серию «подряд» (lock не снимает)."""
    st = _load_state()
    st[RESTORE_ATTEMPTS_KEY] = 0
    _save_state(st)
    return restore_lock_status()


def restore_unlock(reason: str = "manual confirm") -> dict:
    """Ручное подтверждение админом: снять lock и обнулить счётчик."""
    st = _load_state()
    st[RESTORE_LOCK_KEY] = {"active": False, "reason": reason,
                            "ts": int(time.time())}
    st[RESTORE_ATTEMPTS_KEY] = 0
    _save_state(st)
    rc.prune_log(f"restore_lock cleared: {reason}")
    return restore_lock_status()


def boot_ok() -> bool:
    """Последний запуск был успешным (нужно для снятия режима кнопкой)."""
    return bool(_load_state().get("boot_ok"))


def _last_good_snapshot() -> str:
    """Последний годный КОДОВОЙ снимок: не tampered, code_ok, целостность ok.

    Эталон (pinned + L3) предпочитается самому свежему.
    """
    pinned_l3, newest = None, None
    for it in rc.list_backups():          # уже отсортирован: новые вперёд
        if it.get("broken") or not it.get("ok") or it.get("tampered"):
            continue
        if not it.get("code_ok") or not it.get("code_count"):
            continue
        if (it.get("verify") or {}).get("code_ok") is False:
            continue
        if newest is None:
            newest = it["id"]
        if pinned_l3 is None and it.get("pinned") and \
                it.get("verify_level") == "L3":
            pinned_l3 = it["id"]
    return pinned_l3 or newest


def auto_restore(reason: str = "crash") -> dict:
    """§4.1 «Авто»: при падении гварда откат на последний good снимок.

    auto_restore_mode из конфига:
      off  — никогда не предлагать/не делать;
      ask  — записать подсказку state['auto_restore_suggested'] (для UI);
      auto — применить сразу, с автокенарейкой.
    restore_lock выключает авто-восстановление полностью (§4.4).
    """
    lock = restore_lock_status()
    if lock.get("locked"):
        return {"ok": False, "skipped": "restore_lock", "lock": lock}
    cfg = str(rc.config_value("auto_restore_mode", "ask") or "ask").lower()
    candidate = _last_good_snapshot()
    if not candidate:
        return {"ok": False, "skipped": "no_candidate"}
    if cfg == "off":
        return {"ok": False, "skipped": "off", "candidate": candidate}
    if cfg != "auto":
        st = _load_state()
        st["auto_restore_suggested"] = {
            "snapshot": candidate, "reason": reason, "mode": cfg,
            "ts": int(time.time())}
        _save_state(st)
        rc.prune_log(f"auto-restore({cfg}): suggested {candidate} ({reason})")
        return {"ok": False, "suggested": candidate, "mode": cfg}
    report = restore_to_slot(candidate, canary=True)
    report["auto"] = True
    report["suggested"] = candidate
    rc.prune_log(f"auto-restore({reason}): {candidate} -> "
                 f"{'OK' if report.get('ok') else report.get('stage')}")
    return report




