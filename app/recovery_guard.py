#!/usr/bin/env python3
"""Recovery Guard — точка восстановления сервера Infinity.

Запускается ИЗ main.py ПЕРВЫМ (см. блок __main__ в main.py), до старта
приложения, независимо от ошибок в main.py и расширениях.

Что делает:
  * ведёт чекпоинт загрузки расширений (какое расширение грузилось в момент
    краша/подвеса — при следующем старте оно становится «подозрительным»);
  * делает резервные копии критически важных файлов (конфиг профиля, состояние
    расширений, закладки и т.п.) перед каждым стартом;
  * при падении/подвесе приложения на старте — автоматически перезапускает его
    в безопасном режиме (--safe-mode: код расширений не исполняется);
  * пока сервер жив — остаётся запущенным как супервизор: при падении
    перезапускает его (не более 2 рестартов, затем safe-mode), Ctrl+C
    останавливает сервер;
  * умеет восстанавливать критические файлы из резервной копии: --restore <id>.

Процессы:  main.py --server           -> приложение (uvicorn)
           main.py (по умолчанию)     -> запускает этот guard, который
                                          запускает приложение как subprocess
"""

import os
import sys
import json
import time
import glob
import shutil
import argparse
import datetime
import threading
import urllib.request
import urllib.parse
import subprocess
import swallow

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except Exception:
    # Очень старые окружения — fallback (не должно случиться).
    swallow.note('recovery_guard.<module>:L40')
    from http.server import BaseHTTPRequestHandler
    ThreadingHTTPServer = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ИСПРАВЛЕНИЕ 03.10.2026: DATA_DIR жёстко указывал на app\data, где профилей
# нет — реальные данные лежат в <корень>\data (INFINITY_DATA_DIR).
# Из-за этого discover_critical_files() ничего не находил, бэкапы не создавались,
# и список бэкапов на аварийной странице был всегда пуст.
ROOT_DIR = os.environ.get("INFINITY_ROOT") or os.path.dirname(APP_DIR)
DATA_DIR = os.environ.get("INFINITY_DATA_DIR") or os.path.join(ROOT_DIR, "data")

STATE_PATH = os.path.join(DATA_DIR, "recovery_state.json")
BACKUPS_DIR = os.path.join(DATA_DIR, "recovery_backups")
MAX_BACKUPS = 10
BACKUP_MAX_AGE_DAYS = 5   # бэкапы старше 5 дней удаляются
READY_TIMEOUT = 900          # секунд ждём /api/health после старта приложения
MAX_START_ATTEMPTS = 2       # попыток до перехода в safe-mode / отказа
HEALTH_PROBE_TIMEOUT = 4     # таймаут отдельной проверки /api/health
# ИСПРАВЛЕНИЕ 30.09.2026: без потолка guard входил в бесконечный цикл
# рестартов, когда порт держала другая копия сервера (свой процесс падал с
# "порт занят", но /api/health отвечал — и guard считал это своим успехом).
MAX_SUPERVISED_RESTARTS = 3  # перезапусков ПОСЛЕ успешного старта до отказа
RESTART_BACKOFF_MAX = 30     # максимальная пауза между перезапусками, сек
EMERGENCY_LOG = os.path.join(DATA_DIR, "recovery_launch.log")
# Глобальные координаты аварийного интерфейса (заполняются в serve_recovery_ui)
server_port = 8080
# Guard тоже без аутентификации (в нём есть откат файлов), поэтому по
# умолчанию слушаем только localhost; LAN — только явным --host.
server_host = "127.0.0.1"

MANIFEST = "manifest.json"


def log(msg):
    print(f"[recovery-guard] {msg}", flush=True)


# ---------------- состояние ----------------

def load_state():
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"state load error: {e}")

    # Перенос состояния со старого пути (app\data) — иначе после смены
    # DATA_DIR потерялась бы история крашей и подозреваемые расширения.
    legacy = os.path.join(APP_DIR, "data", "recovery_state.json")
    if os.path.exists(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                state = json.load(f)
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            log("state migrated from legacy path: %s" % legacy)
            return state
        except Exception as e:
            log(f"state migrate error: {e}")
    return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        log(f"state save error: {e}")


# ---------------- резервные копии ----------------

def discover_critical_files():
    """Критически важные файлы: конфиг профиля, состояние расширений,
    закладки, индекс. Пути могут меняться между профилями."""
    files = []
    profile_dirs = glob.glob(os.path.join(DATA_DIR, "profiles", "*"))
    for pd in profile_dirs:
        for f in ("config.json", "frontend_state.json"):
            p = os.path.join(pd, f)
            if os.path.exists(p):
                files.append(p)
        ext_dir = os.path.join(pd, "data", "extensions")
        if os.path.isdir(ext_dir):
            for f in ("extensions_state.json", "registry.json"):
                p = os.path.join(ext_dir, f)
                if os.path.exists(p):
                    files.append(p)
    for f in ("bookmarks.json", "share_index.json"):
        p = os.path.join(DATA_DIR, f)
        if os.path.exists(p):
            files.append(p)
    # ИНДЕКС ПРОФИЛЕЙ. Раньше он не попадал в бэкап: profiles_index.json
    # лежит прямо в data/profiles/, а glob выше берёт только config.json и
    # frontend_state.json из подкаталога. При его повреждении система
    # считала, что профилей нет, и создавала новый — все настройки,
    # приложения и расширения «исчезали». Теперь индекс бэкапится.
    idx = os.path.join(DATA_DIR, "profiles", "profiles_index.json")
    if os.path.exists(idx):
        files.append(idx)
    # Соцсеть. Копируем social.db вместе с -wal/-shm, иначе бэкап может
    # оказаться неконсистентным (недавние транзакции лежат в WAL).
    for f in ("social.db", "social.db-wal", "social.db-shm"):
        p = os.path.join(DATA_DIR, f)
        if os.path.exists(p):
            files.append(p)
    return files


def make_backup(force=False):
    """Скопировать критические файлы в BACKUPS_DIR/<ts>/ и вернуть id (ts)."""
    files = discover_critical_files()
    if not files and not force:
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUPS_DIR, ts)
    os.makedirs(dest, exist_ok=True)
    manifest = {"id": ts, "ts": time.time(), "files": []}
    for src in files:
        # "rel" — путь относительно APP_DIR: по нему потом ВОССТАНАВЛИВАЕМ.
        rel = os.path.relpath(src, APP_DIR)
        # ИСПРАВЛЕНИЕ 03.10.2026: dst считался как dest/rel, а rel вида
        # "..\data\profiles\..." уводил копию ЗА пределы каталога бэкапа
        # (все бэкапы сливались в общую recovery_backups\data\).
        # Теперь внутри бэкапа структура своя, безопасная.
        safe = os.path.relpath(src, ROOT_DIR).replace("..", "_up_")
        dst = os.path.join(dest, safe)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            manifest["files"].append({
                "src": src,
                "rel": rel,
                "stored": safe,
            })
        except Exception as e:
            swallow.note('recovery_guard.make_backup:L184', e)
            log(f"backup skip {rel}: {e}")
    with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    _prune_backups()
    return ts


def _prune_backups():
    """Удалить бэкапы старше BACKUP_MAX_AGE_DAYS и лишние сверх MAX_BACKUPS.

    ИСПРАВЛЕНИЕ: каталоги с маркером _COMMIT — это снимки новой системы
    (recovery_core), у них свои лимиты и защита pinned. Старый прунер их
    НЕ трогает, иначе две системы удаляли бы снимки друг друга.
    """
    if not os.path.isdir(BACKUPS_DIR):
        return
    try:
        now = time.time()
        dirs = [
            d for d in os.listdir(BACKUPS_DIR)
            if os.path.isdir(os.path.join(BACKUPS_DIR, d))
            and not os.path.isfile(os.path.join(BACKUPS_DIR, d, "_COMMIT"))
        ]
        kept = []
        for d in sorted(dirs):
            full = os.path.join(BACKUPS_DIR, d)
            age_days = (now - os.path.getmtime(full)) / 86400.0
            if age_days > BACKUP_MAX_AGE_DAYS:
                shutil.rmtree(full, ignore_errors=True)
                log(f"backup pruned (старше {BACKUP_MAX_AGE_DAYS} дн.): {d}")
            else:
                kept.append(d)
        for old in kept[:-MAX_BACKUPS]:
            shutil.rmtree(os.path.join(BACKUPS_DIR, old), ignore_errors=True)
    except Exception as e:
        log(f"prune backups error: {e}")


def list_backups():
    out = []
    try:
        for d in sorted(os.listdir(BACKUPS_DIR)):
            if not os.path.isdir(os.path.join(BACKUPS_DIR, d)):
                continue
            manifest_path = os.path.join(BACKUPS_DIR, d, MANIFEST)
            info = {"id": d, "files": [], "ts": None}
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    info["files"] = [x["rel"] for x in m.get("files", [])]
                    info["ts"] = m.get("ts")
                except Exception:
                    swallow.note('recovery_guard.list_backups:L238')
                    pass
            out.append(info)
    except Exception as e:
        log(f"list backups error: {e}")
    return out


def restore_backup(backup_id):
    """Восстановить критические файлы из резервной копии. Возвращает список."""
    src = os.path.join(BACKUPS_DIR, backup_id)
    if not os.path.isdir(src):
        return {"ok": False, "error": f"Backup {backup_id} not found"}
    restored = []
    manifest_path = os.path.join(src, MANIFEST)
    if not os.path.exists(manifest_path):
        return {"ok": False, "error": f"Backup {backup_id} has no manifest"}
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    for item in manifest.get("files", []):
        rel = item.get("rel", "")
        dst = os.path.join(APP_DIR, rel)
        # Новые бэкапы хранят копию по полю "stored"; в старых его нет,
        # и файл лежал по самому "rel".
        stored = item.get("stored") or rel
        src_file = os.path.join(src, stored)
        if not os.path.exists(src_file):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src_file, dst)
            restored.append(rel)
        except Exception as e:
            log(f"restore error {rel}: {e}")
    return {"ok": True, "restored": restored}


# ---------------- чекпоинт ----------------

def analyze_crash(state):
    """По чекпоинту определить, какое расширение подозревается в краше."""
    changed = False
    checkpoint = state.get("checkpoint")
    boot_ok = bool(state.get("boot_ok", False))
    if checkpoint and not boot_ok:
        suspects = list(state.get("suspects", []))
        if checkpoint not in suspects:
            suspects.append(checkpoint)
            state["suspects"] = suspects
            log(f"crash checkpoint -> suspect: {checkpoint}")
            changed = True
    if boot_ok:
        state["checkpoint"] = None
        changed = True
    if changed:
        save_state(state)
    return changed


# ---------------- запуск приложения ----------------

def _is_healthy(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=HEALTH_PROBE_TIMEOUT
        ) as resp:
            return resp.status == 200
    except Exception:
        swallow.note('recovery_guard._is_healthy:L306')
        return False


def _wait_port_free(port, timeout=15.0):
    """Дождаться, пока порт освободится (старый процесс мог не отдать сокет)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=0.3
            ):
                pass  # кто-то ещё слушает — ждём
        except Exception:
            swallow.note('recovery_guard._wait_port_free:L320')
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                try:
                    s.bind(("127.0.0.1", port))
                    s.close()
                    return True
                except OSError:
                    swallow.note('recovery_guard._wait_port_free:L330')
                    s.close()
            except Exception:
                swallow.note('recovery_guard._wait_port_free:L333')
                return True
        time.sleep(0.5)
    return False


def _app_python():
    """Интерпретатор для запуска main.py.

    Раньше здесь стоял просто sys.executable. Проблема: recovery_guard
    нередко запускается тем же python, что и сам сервер, но НЕ из venv
    проекта. Тогда main.py падал с
        ModuleNotFoundError: No module named 'fastapi'
    guard считал это крашем, уходил в safe-mode, а сам так и оставался
    висеть на порту со страницей восстановления — приложение не
    запускалось вообще.

    Поэтому ищем интерпретатор, в котором реально импортируется
    fastapi, и только если не нашли — падаем обратно на sys.executable.
    """
    root = os.path.dirname(APP_DIR)
    cands = [
        os.path.join(root, "venv", "Scripts", "python.exe"),
        os.path.join(root, ".venv", "Scripts", "python.exe"),
        os.path.join(APP_DIR, "venv", "Scripts", "python.exe"),
    ]
    for exe in cands:
        if not os.path.exists(exe):
            continue
        try:
            r = subprocess.run(
                [exe, "-c", "import fastapi"], capture_output=True, timeout=60)
            if r.returncode == 0:
                if os.path.normcase(exe) != os.path.normcase(sys.executable):
                    log(f"using project interpreter with fastapi: {exe}")
                return exe
            log(f"interpreter has no fastapi, skipping: {exe}")
        except Exception as e:
            swallow.note('recovery_guard._app_python:L371', e)
            log(f"interpreter check failed ({e}): {exe}")
    log("no interpreter with fastapi found; falling back to sys.executable")
    return sys.executable


def _slot_launch_dir():
    """§4: каталог активного слота кода, если он собран, иначе APP_DIR.

    Гвард только читает указатель из state-файла (stdlib, без импорта
    recovery_*), чтобы сломанный код приложения не ломал гвард.
    """
    try:
        st = load_state()
        name = str(st.get("_active_slot", "")).strip().lower()
        if name in ("a", "b"):
            d = os.path.join(DATA_DIR, "recovery_slot_" + name)
            if os.path.isfile(os.path.join(d, "main.py")):
                return d
    except Exception as e:
        swallow.note('recovery_guard._slot_launch_dir:L391', e)
        log(f"slot lookup failed: {e}")
    return APP_DIR


def _app_env(app_dir):
    """Окружение запуска приложения.

    Из слота __file__ указывает на data/recovery_slot_*, поэтому корень и
    данные принудительно закрепляем за настоящими каталогами — иначе
    config_module посчитает корнем dirname(слот) и данные разъедутся.
    """
    env = dict(os.environ)
    if os.path.normcase(os.path.normpath(app_dir)) != \
            os.path.normcase(os.path.normpath(APP_DIR)):
        env.update({
            "INFINITY_ROOT": ROOT_DIR,
            "INFINITY_DATA_DIR": DATA_DIR,
            "INFINITY_APP_DIR": app_dir,
        })
    return env


def start_app(port, host, safe_mode, crash_count):
    """Запустить приложение и дождаться готовности. Возвращает (ok, proc, msg)."""
    state = load_state()
    state["boot_ok"] = False
    state["crash_count"] = crash_count
    state["last_boot_ts"] = time.time()
    state["safe_mode"] = bool(safe_mode)
    save_state(state)

    app_dir = _slot_launch_dir()
    if app_dir != APP_DIR:
        log(f"launching from code slot: {app_dir}")
    cmd = [_app_python(), os.path.join(app_dir, "main.py"), "--server",
           "--port", str(port), "--host", str(host)]
    if safe_mode:
        cmd.append("--safe-mode")
        log("starting app in SAFE MODE (extension code will not run)")
    else:
        log(f"starting app (attempt {crash_count + 1})")

    proc = subprocess.Popen(cmd, cwd=app_dir, env=_app_env(app_dir))
    log(f"spawned app pid={proc.pid}")
    deadline = time.time() + READY_TIMEOUT
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                return False, proc, f"app exited with code {proc.returncode}"
            if _is_healthy(port):
                return True, proc, "app is healthy"
            time.sleep(1)
    except KeyboardInterrupt:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                swallow.note('recovery_guard.start_app:L449')
                pass
        raise
    proc.kill()
    return False, proc, f"app did not become healthy within {READY_TIMEOUT}s (killed)"


# ---------------- аварийный интерфейс восстановления ----------------

def _tail_log(path, n=60):
    """Последние n строк файла (безопасно)."""
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except Exception:
        swallow.note('recovery_guard._tail_log:L467')
        return []


def _find_server_logs():
    """Кандидаты на лог приложения (сервер пишет в ~/.infinity/server.log или server.log)."""
    candidates = [
        os.path.expanduser("~/.infinity/server.log"),
        os.path.join(APP_DIR, "server.log"),
        "/tmp/infinity-server.log",
        "/tmp/infinity_server.log",
        "/tmp/xtts_server_8181.log",
    ]
    return [p for p in candidates if os.path.exists(p)]


def _launch_detached(cmd, app_dir=None):
    """Запустить приложение отдельным процессом (не под супервизором guard'а)."""
    app_dir = app_dir or _slot_launch_dir()
    try:
        os.makedirs(os.path.dirname(EMERGENCY_LOG), exist_ok=True)
        logf = open(EMERGENCY_LOG, "a", encoding="utf-8", errors="replace")
    except Exception:
        swallow.note('recovery_guard._launch_detached:L490')
        logf = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            cmd, cwd=app_dir, env=_app_env(app_dir), stdout=logf,
            stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            logf.close()
        except Exception:
            swallow.note('recovery_guard._launch_detached:L500')
            pass
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        swallow.note('recovery_guard._launch_detached:L504', e)
        return {"ok": False, "error": str(e)}


def _recovery_action(body, port, host):
    """Выполнить действие из аварийного UI. Возвращает JSON-словарь."""
    action = body.get("action", "").strip()
    app_dir = _slot_launch_dir()
    base = [_app_python(), os.path.join(app_dir, "main.py"),
            "--port", str(port), "--host", str(host)]

    if action in ("start_normal", "start_safe", "start_noguard", "start_noguard_ext"):
        # ИСПРАВЛЕНИЕ 03.10.2026: кнопка «отключить все расширения» из
        # аварийной страницы. Ключевое отличие от start_safe — флаг
        # safe_mode НЕ сбрасывается, иначе расширения вернулись бы при
        # следующем старте и сервер снова упал бы.
        keep_safe = action == "start_noguard_ext"
        if action == "start_normal":
            cmd = base + ["--server"]
        elif action in ("start_safe", "start_noguard_ext"):
            cmd = base + ["--server", "--safe-mode"]
        else:
            cmd = base + ["--no-recovery"]  # без guard / recovery
        try:
            st = load_state()
            if not keep_safe:
                st["safe_mode"] = False
                st["safe_mode_locked"] = False
            else:
                st["safe_mode"] = True
                st["safe_mode_locked"] = True
            save_state(st)
        except Exception:
            swallow.note('recovery_guard._recovery_action:L537')
            pass
        res = _launch_detached(cmd)
        log(f"emergency UI: launch {action} -> {res}")
        return res

    if action == "status":
        logs = _find_server_logs()
        return {"ok": True, "state": load_state(),
                "tail": _tail_log(logs[0]) if logs else []}

    if action == "restore":
        backup_id = body.get("backup_id", "")
        if not backup_id:
            return {"ok": False, "error": "backup_id required"}
        res = restore_backup(backup_id)
        if res.get("ok"):
            try:
                st = load_state()
                st["safe_mode"] = False
                st["boot_ok"] = False
                st["suspects"] = []
                save_state(st)
            except Exception:
                swallow.note('recovery_guard._recovery_action:L561')
                pass
        return res

    if action == "list_backups":
        return {"ok": True, "backups": list_backups()}

    return {"ok": False, "error": f"Unknown action: {action}"}


def _recovery_html(port, host, state):
    """HTML-страница аварийного интерфейса."""
    sm = "включён (safe-mode)" if state.get("safe_mode") else "выключен"
    # ИСПРАВЛЕНИЕ 03.10.2026: брали бэкапы из state["backups"], который
    # заполнялся только после падения. На диске бэкапы уже были (если
    # до этого делались), но список на странице оставался пустым.
    backups = list_backups() or (state.get("backups", []) or [])
    bl_opts = "\n".join(
        f"<option value='{b['id']}'>{b['id']} · {len(b.get('files', []))} файлов</option>"
        for b in backups
    )
    # Список файлов по каждому бэкапу — раньше было видно только число.
    rows = []
    for b in backups:
        ts = b.get("ts")
        when = datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "?"
        files = b.get("files", [])
        rows.append(
            f"<div class='bk'><b>{b['id']}</b> · {when} · {len(files)} файлов"
            + (f"<br><span class='muted'>{'<br>'.join(str(x) for x in files[:12])}"
               + (f"<br>… и ещё {len(files) - 12}</span>" if len(files) > 12 else "</span>")
               if files else "")
            + "</div>"
        )
    bl_rows = "\n".join(rows) or "<div class='muted'>Бэкапов пока нет — они появятся после следующего запуска.</div>"
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Инфинити — аварийное восстановление</title>
<style>
*{{box-sizing:border-box;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif}}
/* ИСПРАВЛЕНИЕ 03.10.2026: аварийная страница была тёмной — переделана
   в светлую тему (path.txt). */
body{{margin:0;background:#f4f6fb;color:#1c2333;padding:32px 16px}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{font-size:25px;margin:0 0 4px;color:#12203a}}
.sub{{color:#5b6880;margin:0 0 24px}}
.card{{background:#fff;border:1px solid #e1e6f0;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(20,30,60,.06)}}
h2{{font-size:15px;margin:0 0 12px;color:#243356}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}}
button{{all:unset;box-sizing:border-box;cursor:pointer;text-align:center;padding:12px 14px;border-radius:10px;font-size:14px;font-weight:600;background:#eef2fa;color:#1c2333;border:1px solid #cfd8e8}}
button:hover{{background:#e3eaf7}}
.b-main{{background:#3b6df2;border-color:#3b6df2;color:#fff}}
.b-main:hover{{background:#2f5edb}}
.b-safe{{background:#7a4df2;border-color:#7a4df2;color:#fff}}
.b-safe:hover{{background:#6a3ce0}}
.b-nog{{background:#e2a13b;border-color:#e2a13b;color:#221a06}}
.b-nog:hover{{background:#f0b65c}}
.b-danger{{background:#e04b4b;border-color:#e04b4b;color:#fff}}
.b-danger:hover{{background:#c93a3a}}
code{{display:block;white-space:pre-wrap;background:#f7f9fc;border:1px solid #e1e6f0;border-radius:8px;padding:10px;font-size:12px;max-height:240px;overflow:auto;color:#2f6b41}}
.info{{font-size:13px;color:#3f4a61;line-height:1.6}}
select{{background:#fff;color:#1c2333;border:1px solid #cfd8e8;border-radius:8px;padding:8px;margin-right:8px;max-width:100%}}
.muted{{color:#75809a;font-size:12px}}
.bk{{font-size:13px;color:#2f3a52;background:#f7f9fc;border:1px solid #e1e6f0;border-radius:8px;padding:8px 10px;margin-bottom:8px}}
.bk b{{color:#12203a}}
.warn{{background:#fff7e6;border:1px solid #f0d9a0;border-radius:8px;padding:10px 12px;font-size:12.5px;color:#6b4c07;margin:10px 0 0}}
</style></head><body><div class="wrap">
<h1>⚠️ Сервер не запустился</h1>
<p class="sub">Аварийный интерфейс recovery-guard. Выберите вариант запуска — приложение поднимется на этом же порту.</p>
<div class="card"><h2>Запуск</h2><div class="grid">
<button class="b-main" onclick="go('start_normal')">🚀 Обычный запуск</button>
<button class="b-safe" onclick="go('start_safe')">🛡️ Безопасный (safe-mode)</button>
<button class="b-nog" onclick="go('start_noguard')">⚙️ Без guard / recovery</button>
</div></div>
<div class="card"><h2>Аварийный режим — отключить расширения</h2>
<p class="info">Запустит приложение в safe-mode: расширения не выполняются. Подходит, если сервер падает из-за расширения.</p>
<button class="b-danger" onclick="go('start_noguard_ext')">🚫 Отключить все расширения</button>
<div class="warn">Расширения останутся отключёнными, пока вы не включите их обратно в настройках.</div>
<code id="disable"></code></div>
<div class="card"><h2>Состояние</h2>
<p class="info">safe-mode: <b>{sm}</b> · crash_count: <b>{state.get('crash_count', 0)}</b> ·
подозреваемые: <b>{', '.join(state.get('suspects', [])) or 'нет'}</b></p>
<button onclick="status()">🔄 Обновить статус</button>
<code id="status"></code></div>
<div class="card"><h2>Резервные копии ({len(backups)})</h2>
<div>{bl_rows}</div>
<select id="bk">{bl_opts}</select>
<button onclick="restore()">♻️ Восстановить</button>
<p class="muted">Бэкапы создаются перед стартом, хранятся до {MAX_BACKUPS} штук и удаляются старше {BACKUP_MAX_AGE_DAYS} дн. Каталог: data/recovery_backups</p>
<code id="restore"></code></div>
<div class="card"><h2>Последние строки лога сервера</h2><code id="log"></code></div>
<script>
async function post(a, extra){{const r=await fetch('/action',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.assign({{action:a}},extra||{{}}))}});return await r.json();}}
async function go(a){{const d=await post(a);if(d.ok){{setTimeout(()=>location.href='/',1500);}}else alert('Ошибка: '+(d.error||'?'));}}
async function status(){{const d=await post('status');document.getElementById('status').textContent=JSON.stringify(d.state,null,2);document.getElementById('log').textContent=(d.tail||[]).join('\\n');}}
async function restore(){{const id=document.getElementById('bk').value;if(!id)return;const d=await post('restore',{{backup_id:id}});document.getElementById('restore').textContent=JSON.stringify(d,null,2);}}
status();
</script></div></body></html>"""


class _RecoveryHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # тишина в консоль

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        html = _recovery_html(server_port, server_host, load_state())
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # §6.2: аварийный UI слушает только 127.0.0.1; действия — с localhost
        # или с одноразовым токеном из лога. Плюс rate-limit на POST.
        peer = (self.client_address[0] if self.client_address else "")
        from_local = peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        if not _ui_rate_ok():
            self._json({"ok": False, "error": "слишком много запросов, подождите"},
                       code=429)
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            swallow.note('recovery_guard.do_POST:L702')
            body = {}
        token = str(body.get("token") or self.headers.get("X-Recovery-Token") or "")
        if not from_local and token != _UI_TOKEN:
            self._json({"ok": False, "error": "нужен токен из лога запуска"},
                       code=403)
            return
        self._json(_recovery_action(body, server_port, server_host))


# §6.2: токен действий аварийного UI + простой rate-limit на POST.
_UI_TOKEN = ""
_UI_RATE = {"window": time.time(), "count": 0}
_UI_RATE_MAX = 30          # действий в минуту с одного адреса


def _ui_rate_ok():
    now = time.time()
    if now - _UI_RATE["window"] > 60:
        _UI_RATE["window"] = now
        _UI_RATE["count"] = 0
    _UI_RATE["count"] += 1
    return _UI_RATE["count"] <= _UI_RATE_MAX


def serve_recovery_ui(port, host, state):
    """Поднять аварийный интерфейс и держать его, пока не выберут действие.

    §6.2 (фикс RCE): слушаем ТОЛЬКО 127.0.0.1 — раньше UI поднимался на
    host (0.0.0.0) без авторизации, и любой в локальной сети мог откатить
    код. Действия дополнительно защищены одноразовым токеном и rate-limit.
    """
    global server_port, server_host, _UI_TOKEN
    server_port, server_host = port, host
    _UI_TOKEN = os.urandom(16).hex()
    bind_host = "127.0.0.1"
    log(f"Starting recovery UI on http://{bind_host}:{port} (localhost only)")
    log(f"recovery UI action token: {_UI_TOKEN}")
    try:
        from http.server import ThreadingHTTPServer as _THS
    except Exception:
        swallow.note('recovery_guard.serve_recovery_ui:L743')
        log("Recovery UI unavailable (no ThreadingHTTPServer)")
        return
    httpd = None
    last_err = None
    for _attempt in range(20):
        try:
            httpd = _THS((bind_host, port), _RecoveryHandler)
            break
        except OSError as _e:
            swallow.note('recovery_guard.serve_recovery_ui:L753', _e)
            last_err = _e
            time.sleep(1)
    if httpd is None:
        log(f"Recovery UI bind failed after retries: {last_err}")
        return
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        swallow.note('recovery_guard.serve_recovery_ui:L762')
        pass
    except Exception as e:
        swallow.note('recovery_guard.serve_recovery_ui:L765', e)
        log(f"Recovery UI stopped: {e}")


# ---------------- main ----------------

# --------------------------------------- режим восстановления / авто-откат --

_AUTO_RESTORE_TRIED = False   # за один запуск гварда — не более одной попытки
_CRASH_SNAPSHOT_DONE = False  # §3.6 п.3: снимок при поломке — один раз за запуск


def _snapshot_on_crash(reason="crash"):
    """§3.6 п.3: снять состояние ДО любых действий по восстановлению.

    Минимальный RPO: если сейчас авто-откат или ручной откат перезапишет код,
    текущее (возможно, единственное) состояние должно остаться на диске.
    Уважает флаг snapshot_on_crash; обходит дедуп/интервал. Один раз за запуск.
    """
    global _CRASH_SNAPSHOT_DONE
    if _CRASH_SNAPSHOT_DONE:
        return
    _CRASH_SNAPSHOT_DONE = True
    try:
        import recovery_core as rc
    except Exception as e:
        swallow.note('recovery_guard._snapshot_on_crash:L791', e)
        log(f"snapshot-on-crash: recovery_core недоступен: {e}")
        return
    try:
        snap_id, why = rc.snapshot_on_crash(reason=reason)
        if snap_id:
            log(f"SNAPSHOT-ON-CRASH: {snap_id} ({reason})")
        else:
            log(f"snapshot-on-crash пропущен: {why}")
    except Exception as e:
        swallow.note('recovery_guard._snapshot_on_crash:L801', e)
        log(f"snapshot-on-crash: ошибка: {e}")


def _import_slots():
    """Лениво подключает app/recovery_slots (только stdlib внутри).

    Гвард не обязан импортировать слоты: если модуль сломан/недоступен,
    режимы просто не работают, но гвард остаётся живым.
    """
    try:
        import recovery_slots
        return recovery_slots
    except Exception:
        swallow.note('recovery_guard._import_slots:L815')
        try:
            from app import recovery_slots
            return recovery_slots
        except Exception as e:
            swallow.note('recovery_guard._import_slots:L820', e)
            log(f"recovery_slots недоступен: {e}")
            return None


def _apply_recovery_mode() -> dict:
    """§4.1 «Режим восстановления»: каждый запуск гварда применяет снимок."""
    rs = _import_slots()
    if rs is None:
        return {}
    try:
        rep = rs.recovery_mode_apply()
    except Exception as e:
        swallow.note('recovery_guard._apply_recovery_mode:L833', e)
        log(f"recovery mode: ошибка применения: {e}")
        return {}
    if rep.get("applied"):
        log(f"RECOVERY MODE: снимок применён -> слот {rep.get('active')}")
    elif rep.get("skipped") not in (None, "mode_off"):
        log(f"recovery mode: пропущено ({rep.get('skipped')})")
    return rep


def _reset_restore_attempts() -> None:
    """Успешный старт обнуляет серию неудачных откатов (lock не снимает)."""
    rs = _import_slots()
    if rs is None:
        return
    try:
        rs.restore_attempts_reset()
    except Exception:
        swallow.note('recovery_guard._reset_restore_attempts:L851')
        pass


def _try_auto_restore(reason: str = "start_failed") -> bool:
    """§4.1 «Авто»: перед уходом в аварийку — откат на последний good снимок.

    True → слот переключён, попытку старта надо повторить.
    ask → только подсказка в state (для UI); off → ничего; lock → стоп.
    """
    global _AUTO_RESTORE_TRIED
    # §3.6 п.3: снимок состояния до любых действий по восстановлению.
    _snapshot_on_crash(reason=reason)
    if _AUTO_RESTORE_TRIED:
        return False
    _AUTO_RESTORE_TRIED = True
    rs = _import_slots()
    if rs is None:
        return False
    try:
        rep = rs.auto_restore(reason=reason)
    except Exception as e:
        swallow.note('recovery_guard._try_auto_restore:L873', e)
        log(f"авто-откат: ошибка: {e}")
        return False
    if rep.get("ok"):
        log(f"АВТО-ОТКАТ: снимок {rep.get('id')} -> слот "
            f"{rep.get('active')} ({reason})")
        return True
    if rep.get("suggested"):
        log(f"авто-откат (mode=ask): кандидат {rep.get('suggested')} — "
            f"ожидает подтверждения в UI")
    else:
        log(f"авто-откат пропущен: {rep.get('skipped') or rep.get('stage')}")
    return False



def run_guard(port=8080, host="127.0.0.1", restore=None, force_backup=False,
              restore_once=None):
    state = load_state()

    if restore:
        res = restore_backup(restore)
        if not res.get("ok"):
            log(f"restore failed: {res.get('error')}")
            sys.exit(1)
        log(f"restored {len(res.get('restored', []))} files from backup {restore}")
        state["boot_ok"] = False
        state["suspects"] = []
        save_state(state)

    # §4.1 «Разово»: применить КОДОВОЙ снимок один раз, дальше обычный код.
    # Персистентный режим (recovery_mode) при этом НЕ включается.
    if restore_once:
        rs = _import_slots()
        if rs is None:
            log("restore-once: модуль recovery_slots недоступен")
            sys.exit(1)
        try:
            rep = rs.restore_to_slot(restore_once, canary=True)
        except Exception as e:
            swallow.note('recovery_guard.run_guard:L913', e)
            log(f"restore-once: ошибка: {e}")
            sys.exit(1)
        if not rep.get("ok"):
            log(f"restore-once failed [{rep.get('stage')}]: "
                f"{rep.get('error')}")
            sys.exit(1)
        log(f"restore-once: снимок {restore_once} применён в слот "
            f"{rep.get('active')} (разово; режим восстановления не включался)")

    # Если на порту уже стабильно отвечает работающая копия — выходим,
    # не трогая state и не делая лишний бэкап. 6 проверок подряд (~6с)
    # отличают работающий сервер от «доживающего» (завершающегося) процесса.
    if not restore:
        _healthy_n = 0
        while _healthy_n < 6 and _is_healthy(port):
            _healthy_n += 1
            time.sleep(1)
        if _healthy_n >= 6:
            log("=" * 66)
            log(f"другая копия сервера уже работает на {host}:{port} — "
                f"просто откройте http://localhost:{port}")
            log("=" * 66)
            return

    backup_id = make_backup(force=force_backup)
    if backup_id:
        log(f"backup created: {backup_id}")
        state = load_state()
        backups = list_backups()
        state["backups"] = backups[-MAX_BACKUPS:]
        save_state(state)

    analyze_crash(state)
    state = load_state()
    crash_count = int(state.get("crash_count", 0))
    suspects = state.get("suspects", [])

    # §4.1 «Режим восстановления»: персистентно — каждый запуск гварда
    # применяет закреплённый снимок. Снимается только кнопкой и только
    # после успешного старта (по аналогии с safe_mode_locked).
    _apply_recovery_mode()
    if suspects:
        log(f"suspect extensions: {', '.join(suspects)}")
    if state.get("safe_mode"):
        crash_count = max(crash_count, MAX_START_ATTEMPTS)

    attempt = 0
    crashes_after_ready = 0
    try:
        while True:
            safe_mode = bool(state.get("safe_mode", False)) or crash_count >= MAX_START_ATTEMPTS
            ok, proc, msg = start_app(port, host, safe_mode, crash_count)
            if not ok:
                if _is_healthy(port):
                    # Порт занят, но на нём отвечает работающая копия
                    # (например, её приложение стартовало раньше нашего).
                    log("порт занят работающей копией сервера — выходим без изменений state")
                    state = load_state()
                    state["boot_ok"] = True
                    state["crash_count"] = 0
                    # Раньше здесь safe_mode НЕ сбрасывался. Из-за этого
                    # флаг, однажды включившись, залипал навсегда: сервер
                    # стартовал с --safe-mode при каждом запуске, хотя
                    # crash_count = 0 и checkpoint пуст (крашей не было).
                    # Успешный старт — повод выйти из safe-mode.
                    if state.get("safe_mode") and not state.get("checkpoint"):
                        state["safe_mode"] = False
                        log("выходим из safe-mode: сервер запустился успешно")
                    save_state(state)
                    return
                attempt += 1
                crash_count += 1
                state = load_state()
                state["crash_count"] = crash_count
                save_state(state)
                log(f"start failed: {msg} (attempt {attempt})")
                if not state.get("safe_mode") and attempt < MAX_START_ATTEMPTS:
                    continue
                if not state.get("safe_mode"):
                    state["safe_mode"] = True
                    save_state(state)
                    log("switching to SAFE MODE and retrying once")
                    continue
                # §4.1 «Авто»: попытки исчерпаны — пробуем авто-откат
                if _try_auto_restore("start_attempts_exhausted"):
                    attempt = 0
                    crash_count = 0
                    state = load_state()
                    state["crash_count"] = 0
                    save_state(state)
                    continue
                break
            state = load_state()
            state["boot_ok"] = True
            state["crash_count"] = 0
            crash_count = 0
            state["checkpoint"] = None
            # ИСПРАВЛЕНИЕ 03.10.2026: выход из safe-mode только если его
            # включил НЕ пользователь. Флагу user_locked мы не доверяем:
            # он ставится кнопкой «отключить все расширения», иначе
            # расширения вернулись бы сразу после успешного старта.
            if not state.get("safe_mode_locked"):
                state["safe_mode"] = False  # выходим из аварийного режима
            else:
                log("safe-mode оставлен включённым по выбору пользователя "
                    "(расширения отключены)")
            save_state(state)
            _reset_restore_attempts()   # успех — серия откатов обнуляется
            log("=" * 66)
            log(f"Infinity Search СЕРВЕР ГОТОВ: "
                f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}")
            log("recovery guard is supervising (Ctrl+C to stop)")
            log("=" * 66)
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                swallow.note('recovery_guard.run_guard:L1029')
                if proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait()
                    except Exception:
                        swallow.note('recovery_guard.run_guard:L1035')
                        pass
                log("stopped by user (Ctrl+C)")
                return
            if rc == 0:
                log("server stopped gracefully")
                return
            # ИСПРАВЛЕНИЕ 30.09.2026 (цикл рестартов): наш процесс упал, но порт
            # отвечает здоровым /api/health — значит порт держит ДРУГАЯ копия
            # сервера. Раньше guard считал это своим успешным стартом и уходил
            # в бесконечный цикл рестартов. Здесь просто сдаём контроль.
            if _is_healthy(port):
                log("наш процесс завершился, но порт отвечает — порт держит "
                    "другая копия сервера; рестарты прекращены")
                state = load_state()
                state["boot_ok"] = True
                state["crash_count"] = 0
                save_state(state)
                return
            log(f"server exited unexpectedly (rc={rc}); restarting")
            analyze_crash(load_state())
            crashes_after_ready += 1
            if crashes_after_ready > MAX_SUPERVISED_RESTARTS:
                log(f"приложение падает {crashes_after_ready} раз(а) подряд после "
                    f"успешного старта — прекращаю рестарты, перехожу к аварийке")
                # §4.1 «Авто»: цикл падений после готовности → авто-откат
                if _try_auto_restore("crash_loop_after_ready"):
                    crashes_after_ready = 0
                    state = load_state()
                    state["crash_count"] = 0
                    save_state(state)
                    continue
                break
            # пауза перед рестартом, чтобы не крутить цикл впустую
            _pause = min(2 ** min(crashes_after_ready, 5), RESTART_BACKOFF_MAX)
            log(f"пауза {_pause} c перед рестартом")
            time.sleep(_pause)
            state = load_state()
            # crash_count сбрасывается в state после успешного старта (строка
            # выше), но локальная переменная цикла его не перечитывала: после
            # двух аварийных рестартов каждый следующий поднимал приложение
            # в safe-mode, даже когда state чист (сервер стартовал, счётчик
            # обнулялся, а ветка 965 смотрела на старое значение).
            crash_count = int(state.get("crash_count", 0))
            if not state.get("safe_mode") and crashes_after_ready < MAX_START_ATTEMPTS:
                continue
            if not state.get("safe_mode"):
                state["safe_mode"] = True
                save_state(state)
                log("crash after ready - switching to SAFE MODE and retrying once")
                continue
            # §4.1 «Авто»: последняя попытка перед аварийкой
            if _try_auto_restore("supervise_end"):
                crashes_after_ready = 0
                state = load_state()
                state["crash_count"] = 0
                save_state(state)
                continue
            break
    except KeyboardInterrupt:
        swallow.note('recovery_guard.run_guard:L1089')
        log("stopped by user")
        return

    state = load_state()
    state["safe_mode"] = True
    save_state(state)
    # §3.6 п.3: если авто-откат не срабатывал (off/lock) — всё равно снимаем
    # текущее состояние перед показом аварийки и любыми ручными откатами.
    _snapshot_on_crash(reason="emergency_ui")
    log("=" * 60)
    log("SERVER FAILED TO START. Recovery UI on http://{0}:{1}".format(host, port))
    log("Backups:       python {0} --list-backups".format(os.path.basename(__file__)))
    log("Restore:       python {0} --restore <id>".format(os.path.basename(__file__)))
    log("Start (safe):  python main.py --safe-mode")
    log("=" * 60)
    # Ждём освобождения порта: убитый процесс мог не успеть отдать сокет,
    # а на порту могла остаться другая копия сервера (иначе recovery UI
    # падает с "Address already in use").
    if not _wait_port_free(port, timeout=15.0):
        log(f"port {port} still busy after waiting — recovery UI may fail to bind")
    # Вместо простого выхода показываем аварийный интерфейс на том же порту.
    serve_recovery_ui(port, host, load_state())
    sys.exit(0)


def print_status():
    state = load_state()
    print(json.dumps({
        "safe_mode": state.get("safe_mode", False),
        "boot_ok": state.get("boot_ok", False),
        "checkpoint": state.get("checkpoint"),
        "suspects": state.get("suspects", []),
        "blocked": state.get("blocked", []),
        "crash_count": state.get("crash_count", 0),
        "boot_errors": state.get("boot_errors", []),
        "last_boot_ts": state.get("last_boot_ts"),
        "backups": state.get("backups", []),
    }, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Recovery Guard")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--restore", default=None, help="Восстановить из бэкапа <id>")
    ap.add_argument("--restore-once", default=None, dest="restore_once",
                    help="Применить КОДОВОЙ снимок <id> один раз (без режима)")
    ap.add_argument("--status", action="store_true", help="Показать состояние")
    ap.add_argument("--list-backups", action="store_true")
    ap.add_argument("--force-backup", action="store_true")
    a = ap.parse_args()

    if a.status:
        print_status()
        return
    if a.list_backups:
        for b in list_backups():
            print(f"{b['id']}  {len(b['files'])} files")
        return
    run_guard(port=a.port, host=a.host, restore=a.restore,
              force_backup=a.force_backup, restore_once=a.restore_once)


if __name__ == "__main__":
    main()