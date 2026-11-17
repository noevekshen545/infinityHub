#!/usr/bin/env python3
"""
load_daemon — ресурсно-адаптивный менеджер фоновой нагрузки (guard.md §8).

Решает три проблемы:
1. GIL: CPU-тяжёлые задачи (индексация, парсинг) запускаются ОТДЕЛЬНЫМИ
   процессами (python load_jobs.py ...) с пониженным приоритетом — сервер
   остаётся отзывчивым.
2. Приоритеты и троттлинг: 5 уровней (critical > high > normal > low > idle).
   critical/high выполняются сразу; остальные — через взвешенную очередь с
   адаптивным лимитом параллелизма (в условных единицах стоимости, а не «по
   штукам»). Anti-starvation: задача, ждущая дольше stale_after, повышает
   приоритет. При перегрузке ПК (CPU/RAM/IO-wait) запуск фоновых задач
   приостанавливается с гистерезисом.
3. Retry с экспоненциальным backoff, timeout, классификация ошибок; 3 провала
   по ключу → карантин 10 мин. Ограниченный пул потоков вместо «поток на
   задачу». Персистентность очереди process-задач. Метрики (EWMA, ETA).

Публичный API совместим со старым: submit / submit_job / cancel / status /
get_daemon / PROFILE.

Использование:
    import load_daemon
    load_daemon.submit_job("rebuild_search_index", job_args={"force_full": False},
                           key="index-rebuild")
    load_daemon.submit(func, name="news-fetch", key="news-fetch", priority="low")

Статус: load_daemon.status()  →  /api/load-daemon/status
"""
import os
import sys
import json
import time
import uuid
import heapq
import logging
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
import swallow

logger = logging.getLogger("infinity")

try:
    import psutil
    _PSUTIL = True
except Exception:
    swallow.note('load_daemon.<module>:L47')
    psutil = None
    _PSUTIL = False

try:
    import recovery_core as rc
except Exception:
    swallow.note('load_daemon.<module>:L54')
    rc = None

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = (rc.DATA_DIR if rc else
             os.path.join(os.path.dirname(_BASE_DIR), "data"))
_STATE_DIR = os.path.join(_DATA_DIR, "load_daemon")
os.makedirs(_STATE_DIR, exist_ok=True)
_QUEUE_FILE = os.path.join(_STATE_DIR, "queue.json")

# 5 приоритетов (guard.md §8). Старые значения high/low остаются валидными.
PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3, "idle": 4}
VALID_PRIORITIES = tuple(PRIORITY_RANK)
# Немедленный запуск вне бюджета параллелизма.
URGENT = ("critical", "high")

# Взвешенная стоимость задач в условных единицах (бюджет = base_concurrency).
COST = {"process": 3.0, "thread": 0.5, "network": 0.05}

QUARANTINE_SEC = 600          # 3 провала по ключу → карантин
MAX_RETRIES_DEFAULT = {"process": 2, "thread": 0}


def _cfg(dotted, default):
    if rc is None:
        return default
    try:
        v = rc.config_value(dotted, default)
        return default if v is None else v
    except Exception:
        swallow.note('load_daemon._cfg:L84')
        return default


# ----------------------------------------------------------------------------
# Профиль железа
# ----------------------------------------------------------------------------

class ResourceProfile:
    """Определяет мощность ПК один раз при старте; следит за загрузкой."""

    def __init__(self):
        self.cpu_count = os.cpu_count() or 4
        self.total_ram_gb = 8.0
        if _PSUTIL:
            try:
                self.total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            except Exception:
                swallow.note('load_daemon.__init__:L102')
                pass
        if self.cpu_count >= 8 and self.total_ram_gb >= 14:
            self.tier = "powerful"
            self.base_concurrency = min(6, max(2, self.cpu_count // 2))
            self.cpu_pause_threshold = 92
            self.ram_pause_floor_gb = 0.5
        elif self.cpu_count >= 4 and self.total_ram_gb >= 7:
            self.tier = "mid"
            self.base_concurrency = 2
            self.cpu_pause_threshold = 82
            self.ram_pause_floor_gb = 1.0
        else:
            self.tier = "weak"
            self.base_concurrency = 1
            self.cpu_pause_threshold = 70
            self.ram_pause_floor_gb = 1.5
        self._proc = None
        if _PSUTIL:
            try:
                self._proc = psutil.Process(os.getpid())
            except Exception:
                swallow.note('load_daemon.__init__:L124')
                self._proc = None

    def snapshot(self):
        cpu = 0.0
        avail_gb = 0.0
        iowait = 0.0
        self_cpu = 0.0
        if _PSUTIL:
            try:
                cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                avail_gb = vm.available / (1024 ** 3)
                try:
                    ct = psutil.cpu_times_percent(interval=None)
                    iowait = getattr(ct, "iowait", 0.0) or 0.0
                except Exception:
                    swallow.note('load_daemon.snapshot:L141')
                    iowait = 0.0
            except Exception:
                swallow.note('load_daemon.snapshot:L144')
                pass
            if self._proc:
                try:
                    self_cpu = self._proc.cpu_percent(interval=None)
                except Exception:
                    swallow.note('load_daemon.snapshot:L150')
                    self_cpu = 0.0
        return {
            "cpu_percent": round(cpu, 1),
            "self_cpu_percent": round(self_cpu, 1),
            "io_wait": round(iowait, 1),
            "avail_ram_gb": round(avail_gb, 2),
            "total_ram_gb": round(self.total_ram_gb, 2),
            "cpu_count": self.cpu_count,
            "tier": self.tier,
        }

    def overloaded(self):
        """True — система занята, фоновые (не urgent) задачи не запускать."""
        snap = self.snapshot()
        if snap["cpu_percent"] >= self.cpu_pause_threshold:
            return True
        if snap["avail_ram_gb"] and snap["avail_ram_gb"] < self.ram_pause_floor_gb:
            return True
        if snap["io_wait"] >= 60:        # диск встал — демон это заметит
            return True
        return False


PROFILE = ResourceProfile()

# ----------------------------------------------------------------------------
# Демон
# ----------------------------------------------------------------------------

class _Task:
    __slots__ = ("id", "name", "key", "priority", "mode", "func", "args",
                 "kwargs", "job", "job_args", "delay", "on_complete",
                 "seq", "status", "progress", "message", "started_at",
                 "finished_at", "queued_at", "error", "result", "proc",
                 "cancel_flag", "cost", "attempts", "max_retries", "timeout",
                 "next_retry_at", "fail_reason")

    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.proc = None
        self.cancel_flag = False
        self.attempts = 0
        self.max_retries = 0
        self.timeout = None
        self.next_retry_at = 0.0
        self.cost = COST["thread"]
        self.queued_at = time.time()
        self.fail_reason = None

    def eff_rank(self):
        """Приоритет с учётом anti-starvation: долго ждёт — повышаем."""
        base = PRIORITY_RANK.get(self.priority, 3)
        stale = max(10, int(_cfg("daemon.stale_after_sec", 120)))
        waited = time.time() - (self.queued_at or 0)
        boost = int(waited // max(1, stale))
        return max(0, base - boost)

    def __lt__(self, other):
        try:
            return (self.eff_rank(), self.seq) < (other.eff_rank(), other.seq)
        except Exception:
            swallow.note('load_daemon.__lt__:L212')
            return False


class LoadDaemon:
    def __init__(self):
        self._heap = []
        self._tasks = {}
        self._keys = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._running_cost = 0.0
        self._seq = 0
        self._history = []
        self._stop = False
        self._paused = False
        self._gov_paused = False
        self._interactive = False
        self._quarantine = {}          # key -> until_ts
        self._fail_counts = {}         # key -> consecutive fails
        self._ewma = {}                # name -> avg duration
        self._pool = ThreadPoolExecutor(max_workers=16,
                                        thread_name_prefix="load-daemon")
        self._restore_queue()
        threading.Thread(target=self._dispatch_loop, daemon=True,
                         name="load-daemon-dispatch").start()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="load-daemon-watchdog").start()
        logger.info("[load_daemon] started: tier=%s cpu=%s ram=%.1fGB budget=%s",
                    PROFILE.tier, PROFILE.cpu_count, PROFILE.total_ram_gb,
                    self._base_budget())

    # ---------------- публичное API ----------------

    def submit(self, func, *args, name=None, key=None, priority="low",
               delay=0.0, on_complete=None, cost=None, timeout=None,
               max_retries=None, **kwargs):
        t = self._new_task(name or getattr(func, "__name__", "task"), key,
                           priority, "thread", on_complete=on_complete)
        t.func = func
        t.args = args
        t.kwargs = kwargs
        t.cost = float(cost) if cost is not None else COST["thread"]
        t.max_retries = (MAX_RETRIES_DEFAULT["thread"] if max_retries is None
                         else int(max_retries))
        t.timeout = timeout
        return self._register(t)

    def submit_job(self, job, job_args=None, name=None, key=None,
                   priority="low", delay=0.0, on_complete=None, cost=None,
                   timeout=None, max_retries=None):
        t = self._new_task(name or job, key, priority, "process",
                           on_complete=on_complete)
        t.job = job
        t.job_args = dict(job_args or {})
        t.cost = float(cost) if cost is not None else COST["process"]
        t.max_retries = (MAX_RETRIES_DEFAULT["process"] if max_retries is None
                         else int(max_retries))
        t.timeout = timeout
        return self._register(t)

    def set_interactive(self, on):
        """Интерактивный режим: открыт браузер/идёт поиск — фон уступает."""
        self._interactive = bool(on)
        with self._cond:
            self._cond.notify_all()

    def set_governor_pause(self, on):
        """Пауза фона по решению губернатора ресурсов (режим Hard).

        Отдельный флаг, а не `_paused`: автоматический бюджет снимает свою
        паузу сам, и он не должен отменять решение управляющего контура.
        """
        on = bool(on)
        with self._cond:
            if self._gov_paused != on:
                self._gov_paused = on
                logger.info("[load_daemon] фон %s губернатором",
                            "приостановлен" if on else "возобновлён")
            self._cond.notify_all()
        return on

    def pressure(self):
        """Компактный срез очереди для губернатора."""
        with self._lock:
            queued = len(self._heap)
            running = len(self._tasks)
            cost = round(self._running_cost, 2)
            paused = bool(self._paused or self._gov_paused)
            quarantine = len(self._quarantine)
        try:
            budget = round(float(self._budget()), 2)
        except Exception:
            swallow.note('load_daemon.pressure:L300')
            budget = 0.0
        return {"queued": queued, "running": running, "cost": cost,
                "budget": budget, "paused": paused, "quarantine": quarantine}

    def cancel(self, task_id):
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return {"error": "task not found"}
            if t.status in ("done", "cancelled"):
                return {"ok": False, "message": "уже завершена"}
            t.cancel_flag = True
            if t.proc and t.proc.poll() is None:
                try:
                    t.proc.terminate()
                except Exception:
                    swallow.note('load_daemon.cancel:L290')
                    pass
            if t.status == "queued":
                if t.key and self._keys.get(t.key) == t.id:
                    self._keys.pop(t.key, None)
                t.status = "cancelled"
                t.finished_at = time.time()
                t.message = "Отменена в очереди"
                self._history.append(self._tinfo(t))
                del self._history[:-50]
                self._persist_queue()
            return {"ok": True}

    def status(self):
        with self._lock:
            snap = PROFILE.snapshot()
            running = [self._tinfo(t) for t in self._tasks.values()
                       if t.status == "running"]
            queued = [self._tinfo(t) for t in sorted(self._heap)]
            stale = max(10, int(_cfg("daemon.stale_after_sec", 120)))
            starving = [t.id for t in self._heap
                        if time.time() - (t.queued_at or 0) > stale]
            now = time.time()
            quarantined = [{"key": k, "until": int(v)}
                           for k, v in self._quarantine.items() if v > now]
            return {
                "resources": snap,
                "limits": {
                    "base_concurrency": PROFILE.base_concurrency,
                    "budget": self._base_budget(),
                    "effective_concurrency": max(0, self._budget()),
                    "paused_by_load": self._paused,
                    "interactive": self._interactive,
                    "running_cost": round(self._running_cost, 2),
                },
                "running": running,
                "queued": queued,
                "starving": starving,
                "quarantined": quarantined,
                "metrics": {"ewma": dict(self._ewma)},
                "recent": list(self._history[-20:]),
            }

    # ---------------- внутреннее ----------------

    def _new_task(self, name, key, priority, mode, on_complete=None):
        t = _Task()
        t.name = name
        t.key = key
        t.priority = priority if priority in VALID_PRIORITIES else "low"
        t.mode = mode
        t.func = None
        t.args = ()
        t.kwargs = {}
        t.job = None
        t.job_args = None
        t.delay = 0.0
        t.on_complete = on_complete
        t.status = "queued"
        t.progress = 0
        t.message = "В очереди"
        t.started_at = None
        t.finished_at = None
        t.error = None
        t.result = None
        return t

    def _register(self, t, delay=0.0):
        t.delay = float(delay or 0)
        with self._lock:
            if t.key and self._quarantine.get(t.key, 0) > time.time():
                logger.info("[load_daemon] quarantine: '%s' пропущена", t.key)
                return None
            if t.key and t.key in self._keys:
                existing_id = self._keys[t.key]
                logger.info("[load_daemon] dedup: '%s' уже в работе (%s)",
                            t.key, existing_id)
                return existing_id
            self._seq += 1
            t.seq = self._seq
            self._tasks[t.id] = t
            if t.key:
                self._keys[t.key] = t.id
            heapq.heappush(self._heap, t)
            self._persist_queue()
            self._cond.notify_all()
        logger.info("[load_daemon] queued: %s (%s, prio=%s, mode=%s)",
                    t.name, t.id, t.priority, t.mode)
        return t.id

    def _base_budget(self):
        """Бюджет параллелизма в условных единицах стоимости."""
        mc = _cfg("daemon.max_concurrency", "auto")
        if isinstance(mc, int):
            return float(mc)
        return float(PROFILE.base_concurrency)

    def _budget(self):
        """Адаптивный бюджет с гистерезисом и учётом IO-wait/своей нагрузки."""
        if self._gov_paused:
            return 0.0
        base = self._base_budget()
        if self._interactive:
            base = max(0.0, base - 1.0)
        snap = PROFILE.snapshot()
        hi = PROFILE.cpu_pause_threshold
        lo = max(0, hi - 10)
        ram_low = snap["avail_ram_gb"] and \
            snap["avail_ram_gb"] < PROFILE.ram_pause_floor_gb
        io_high = snap["io_wait"] >= 60
        cpu_high = snap["cpu_percent"] >= hi or snap["self_cpu_percent"] >= hi * 1.5
        if not self._paused and (cpu_high or ram_low or io_high):
            self._paused = True
        elif self._paused and snap["cpu_percent"] < lo and not ram_low \
                and snap["io_wait"] < 30:
            self._paused = False
        return 0.0 if self._paused else base

    def _dispatch_loop(self):
        while not self._stop:
            with self._cond:
                while True:
                    now = time.time()
                    budget = self._budget()
                    ready = [t for t in self._heap
                             if not t.cancel_flag and t.delay <= now
                             and t.next_retry_at <= now]
                    started_any = False
                    for t in sorted(ready):
                        if t.priority in URGENT:
                            self._pop(t)
                            self._start_task(t)
                            started_any = True
                            continue
                        if budget > 0 and self._running_cost + t.cost <= budget:
                            self._pop(t)
                            self._start_task(t)
                            budget -= t.cost
                            started_any = True
                    if started_any:
                        continue
                    future = [min(t.delay, t.next_retry_at) - now
                              for t in self._heap
                              if t.delay > now or t.next_retry_at > now]
                    timeout = min([2.0] + [f for f in future if f > 0]) \
                        if future else 2.0
                    self._cond.wait(max(0.2, timeout))
                    break

    def _pop(self, t):
        try:
            self._heap.remove(t)
            heapq.heapify(self._heap)
        except ValueError:
            swallow.note('load_daemon._pop:L442')
            pass

    def _watchdog_loop(self):
        while not self._stop:
            time.sleep(5)
            with self._cond:
                self._cond.notify_all()

    def _start_task(self, t):
        t.status = "running"
        t.attempts += 1
        t.started_at = time.time()
        t.message = "Выполняется"
        self._running_cost += t.cost
        self._pool.submit(self._run_wrapper, t)

    def _run_wrapper(self, t):
        try:
            if t.mode == "process":
                result = self._run_subprocess(t)
            else:
                result = self._run_with_timeout(t)
            if t.cancel_flag:
                t.status = "cancelled"
                t.message = "Отменена"
            else:
                t.status = "done"
                t.progress = 100
                t.message = "Завершено"
                t.result = result
                self._record_success(t)
        except Exception as e:
            if t.cancel_flag:
                t.status = "cancelled"
                t.message = "Отменена"
            else:
                t.fail_reason = self._classify(e)
                if t.attempts <= t.max_retries:
                    self._schedule_retry(t, e)
                    return
                logger.error("[load_daemon] task %s (%s) error: %s",
                             t.name, t.id, e, exc_info=True)
                t.status = "error"
                t.message = f"Ошибка: {e}"
                t.error = str(e)
                self._record_failure(t)
        finally:
            if t.status in ("done", "error", "cancelled"):
                t.finished_at = time.time()
                with self._lock:
                    self._running_cost -= t.cost
                    self._tasks.pop(t.id, None)
                    if t.key and self._keys.get(t.key) == t.id:
                        self._keys.pop(t.key, None)
                    self._history.append(self._tinfo(t))
                    del self._history[:-50]
                    self._persist(t)
                    self._persist_queue()
                    self._cond.notify_all()
                if t.on_complete and t.status == "done":
                    try:
                        t.on_complete(t)
                    except Exception as e:
                        logger.warning("[load_daemon] on_complete error: %s", e)

    def _run_with_timeout(self, t):
        """Поточная задача с таймаутом: функция выполняется в worker-потоке,
        ожидание ограничено timeout."""
        if not t.timeout:
            return t.func(*t.args, **(t.kwargs or {}))
        box = {}

        def _call():
            try:
                box["r"] = t.func(*t.args, **(t.kwargs or {}))
            except BaseException as e:  # noqa: BLE001
                swallow.note('load_daemon._call:L519', e)
                box["e"] = e
        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(float(t.timeout))
        if th.is_alive():
            raise TimeoutError(f"timeout {t.timeout}s")
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def _schedule_retry(self, t, err):
        backoff = min(60, 2 ** t.attempts)
        t.next_retry_at = time.time() + backoff
        t.status = "queued"
        t.message = f"Повтор #{t.attempts} через {backoff}s ({t.fail_reason})"
        with self._lock:
            self._running_cost -= t.cost
            heapq.heappush(self._heap, t)
            self._cond.notify_all()
        logger.warning("[load_daemon] retry %s (%s) in %ss: %s",
                       t.name, t.id, backoff, t.fail_reason)

    @staticmethod
    def _classify(err):
        if isinstance(err, TimeoutError):
            return "timeout"
        if isinstance(err, PermissionError):
            return "permission"
        if isinstance(err, (FileNotFoundError,)):
            return "missing_file"
        return "runtime"

    def _record_success(self, t):
        dur = max(0.0, time.time() - (t.started_at or time.time()))
        prev = self._ewma.get(t.name, dur)
        self._ewma[t.name] = round(0.7 * prev + 0.3 * dur, 2)
        if t.key:
            self._fail_counts.pop(t.key, None)

    def _record_failure(self, t):
        if not t.key:
            return
        n = self._fail_counts.get(t.key, 0) + 1
        self._fail_counts[t.key] = n
        if n >= 3:
            self._quarantine[t.key] = time.time() + QUARANTINE_SEC
            self._fail_counts.pop(t.key, None)
            logger.warning("[load_daemon] quarantine key '%s' на %ss",
                           t.key, QUARANTINE_SEC)

    def _tinfo(self, t):
        eta = None
        if t.status == "queued":
            avg = self._ewma.get(t.name)
            if avg:
                eta = int(avg)
        return {
            "id": t.id, "name": t.name, "key": t.key, "priority": t.priority,
            "mode": t.mode, "status": t.status, "progress": t.progress,
            "message": t.message, "started_at": t.started_at,
            "finished_at": t.finished_at, "error": t.error,
            "cost": t.cost, "attempts": t.attempts,
            "fail_reason": t.fail_reason, "eta_sec": eta,
        }

    def _persist(self, t):
        try:
            fp = os.path.join(_STATE_DIR, f"{t.id}.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(self._tinfo(t), f, ensure_ascii=False)
        except Exception:
            swallow.note('load_daemon._persist:L591')
            pass

    def _persist_queue(self):
        """Сохранить process-задачи из очереди (func-задачи сериализовать
        нельзя). Восстанавливаются после рестарта."""
        try:
            items = []
            for t in self._heap:
                if t.mode == "process" and t.job:
                    items.append({"job": t.job, "job_args": t.job_args,
                                  "name": t.name, "key": t.key,
                                  "priority": t.priority, "cost": t.cost,
                                  "delay": max(0.0, t.delay - time.time())})
            tmp = _QUEUE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            os.replace(tmp, _QUEUE_FILE)
        except Exception:
            swallow.note('load_daemon._persist_queue:L610')
            pass

    def _restore_queue(self):
        try:
            if not os.path.isfile(_QUEUE_FILE):
                return
            with open(_QUEUE_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
            for it in items:
                if not isinstance(it, dict) or not it.get("job"):
                    continue
                self.submit_job(it["job"], job_args=it.get("job_args") or {},
                                name=it.get("name"), key=it.get("key"),
                                priority=it.get("priority", "low"),
                                delay=it.get("delay", 0))
            if items:
                logger.info("[load_daemon] восстановлено задач из очереди: %d",
                            len(items))
        except Exception as e:
            logger.warning("[load_daemon] restore queue error: %s", e)

    def _run_subprocess(self, t):
        """Запустить load_jobs.py отдельным процессом с низким приоритетом."""
        cmd = [sys.executable, os.path.join(_BASE_DIR, "load_jobs.py"), t.job]
        for k, v in (t.job_args or {}).items():
            cmd += [f"--{k}", str(v)]
        log_path = os.path.join(_STATE_DIR, f"{t.id}.log")
        creationflags = 0
        if sys.platform == "win32":
            # idle — минимальный приоритет ОС; иначе ниже нормального.
            creationflags = 0x00000040 if t.priority == "idle" \
                else 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
        with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                    cwd=_BASE_DIR, creationflags=creationflags)
        if _PSUTIL and sys.platform != "win32":
            try:
                psutil.Process(proc.pid).nice(
                    15 if t.priority == "idle" else 10)
            except Exception:
                swallow.note('load_daemon._run_subprocess:L651')
                pass
        with self._lock:
            t.proc = proc
        try:
            rc_code = proc.wait(timeout=t.timeout) if t.timeout else proc.wait()
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                swallow.note('load_daemon._run_subprocess:L661')
                pass
            raise TimeoutError(f"process timeout {t.timeout}s")
        if rc_code != 0 and not t.cancel_flag:
            raise RuntimeError(f"worker exited with code {rc_code} (лог: {log_path})")
        result = {"ok": True}
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("RESULT:"):
                        result = json.loads(line[len("RESULT:"):].strip())
        except Exception:
            swallow.note('load_daemon._run_subprocess:L673')
            pass
        return result


_DAEMON = None
_DAEMON_LOCK = threading.Lock()


def get_daemon() -> LoadDaemon:
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is None:
            _DAEMON = LoadDaemon()
        return _DAEMON


def submit(func, *a, **kw):
    return get_daemon().submit(func, *a, **kw)


def submit_job(job, job_args=None, **kw):
    return get_daemon().submit_job(job, job_args=job_args, **kw)


def cancel(task_id):
    return get_daemon().cancel(task_id)


def status():
    return get_daemon().status()


def set_interactive(on):
    return get_daemon().set_interactive(on)


def set_governor_pause(on):
    return get_daemon().set_governor_pause(on)


def pressure():
    return get_daemon().pressure()


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
