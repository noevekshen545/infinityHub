"""
ASIO Scheduled Tasks Module
Агент создаёт отложенные задачи с расписанием.
- Одноразовые: через N минут/часов, или в конкретное время
- Интервальные: повторять каждые N минут/часов
- Агент создаёт задачи через инструмент [TOOL:schedule_task]
"""

import os
import json
import time
import uuid
import threading
import logging
import subprocess
import re
from typing import Dict, List, Any, Optional, Callable
from datetime import datetime, timedelta
import swallow

logger = logging.getLogger("infinity")

AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
SCHEDULED_DIR = os.path.join(AGENT_BASE, "scheduled_tasks")
os.makedirs(SCHEDULED_DIR, exist_ok=True)


def _current_profile_id_sched() -> str:
    """ID активного профиля (пустая строка — общий/без профиля)."""
    try:
        import profiles_module as PM
        cur = PM.get_active_profile()
        return str(cur.get("id", "")) if cur else ""
    except Exception:
        swallow.note('agent_scheduler_tasks._current_profile_id_sched:L34')
        return ""

# Running scheduler thread
_scheduler_thread = None
_tasks_cache = {}


class ScheduledTask:
    def __init__(self, task_id: str, prompt: str, model: str = None,
                 run_type: str = "once", run_at: float = None,
                 interval_minutes: int = 60, max_runs: int = 10,
                 session_id: str = None, daily_time: str = "",
                 weekday: int = None, cron: str = ""):
        self.id = task_id
        self.prompt = prompt
        self.model = model
        self.run_type = run_type  # "once", "interval", "at_time", "daily", "cron"
        self.run_at = run_at  # Unix timestamp
        self.interval_minutes = interval_minutes
        self.max_runs = max_runs
        self.run_count = 0
        self.session_id = session_id
        self.daily_time = daily_time  # "HH:MM" для run_type="daily"
        self.weekday = weekday  # 0=Пн..6=Вс — день недели для daily (None = каждый день)
        self.cron = cron  # cron-выражение "мин час дом мес дн" для run_type="cron"
        self.profile_id = _current_profile_id_sched()
        self.status = "pending"  # pending, running, completed, failed, cancelled
        self.created_at = time.time()
        self.last_run = None
        self.last_result = None
        self.next_run = run_at
        self.retry_count = 0
        self.max_retries = 3
        self.execution_history = []

    def to_dict(self):
        return {
            "id": self.id,
            "prompt": self.prompt,
            "model": self.model,
            "run_type": self.run_type,
            "run_at": self.run_at,
            "interval_minutes": self.interval_minutes,
            "max_runs": self.max_runs,
            "run_count": self.run_count,
            "session_id": self.session_id,
            "daily_time": self.daily_time,
            "weekday": self.weekday,
            "cron": self.cron,
            "profile_id": self.profile_id,
            "status": self.status,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "next_run": self.next_run,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "execution_history": self.execution_history[-20:]
        }

    @classmethod
    def from_dict(cls, data):
        t = cls(
            data["id"], data["prompt"], data.get("model"),
            data.get("run_type", "once"), data.get("run_at"),
            data.get("interval_minutes", 60), data.get("max_runs", 10),
            data.get("session_id"), data.get("daily_time", ""),
            data.get("weekday"), data.get("cron", "")
        )
        t.run_count = data.get("run_count", 0)
        t.status = data.get("status", "pending")
        t.profile_id = data.get("profile_id", "")
        t.created_at = data.get("created_at", time.time())
        t.last_run = data.get("last_run")
        t.last_result = data.get("last_result")
        t.next_run = data.get("next_run")
        t.retry_count = data.get("retry_count", 0)
        t.max_retries = data.get("max_retries", 3)
        t.execution_history = data.get("execution_history", [])
        return t


def load_tasks() -> List[ScheduledTask]:
    tasks = []
    if not os.path.exists(SCHEDULED_DIR):
        return tasks
    for fn in os.listdir(SCHEDULED_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(SCHEDULED_DIR, fn), encoding="utf-8") as f:
                    data = json.load(f)
                task = ScheduledTask.from_dict(data)
                # Краш во время выполнения: задача зависла в running - вернуть в pending
                if task.status == "running":
                    stale_after = 1800  # 30 минут без обновления
                    last_touch = task.last_run or task.created_at
                    if time.time() - last_touch > stale_after:
                        task.status = "pending"
                        task.retry_count += 1
                        logger.warning(f"[SCHEDULED] Recovered stale running task: {task.id}")
                        save_task(task)
                tasks.append(task)
            except Exception as e:
                logger.warning(f"Failed to load task {fn}: {e}")
    tasks.sort(key=lambda t: t.next_run or float('inf'))
    return tasks


def save_task(task: ScheduledTask):
    fp = os.path.join(SCHEDULED_DIR, f"{task.id}.json")
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, fp)


def delete_task(task_id: str) -> bool:
    fp = os.path.join(SCHEDULED_DIR, f"{task_id}.json")
    if os.path.exists(fp):
        os.remove(fp)
        return True
    return False


_WEEKDAY_WORDS = {
    "понедельник": 0, "понедельникам": 0, "вторник": 1, "вторникам": 1,
    "среду": 2, "среда": 2, "средам": 2, "четверг": 3, "четвергам": 3,
    "пятницу": 4, "пятницам": 4, "пятница": 4, "субботу": 5, "субботам": 5,
    "суббота": 5, "воскресенье": 6, "воскресеньям": 6,
    "monday": 0, "mondays": 0, "tuesday": 1, "tuesdays": 1, "wednesday": 2,
    "wednesdays": 2, "thursday": 3, "thursdays": 3, "friday": 4, "fridays": 4,
    "saturday": 5, "saturdays": 5, "sunday": 6, "sundays": 6,
}


def _cron_field_match(field: str, value: int) -> bool:
    """Совпадение одного поля cron: '*', 'N', '*/S', 'A-B', списки 'A,B'."""
    for item in str(field).split(","):
        item = item.strip()
        if not item:
            continue
        if item == "*":
            return True
        if item.startswith("*/"):
            try:
                step = int(item[2:] or 1)
                return value % step == 0
            except Exception:
                swallow.note('agent_scheduler_tasks._cron_field_match:L185')
                return False
        if "-" in item:
            try:
                a, b = item.split("-", 1)
                return int(a) <= value <= int(b)
            except Exception:
                swallow.note('agent_scheduler_tasks._cron_field_match:L192')
                return False
        try:
            v = int(item)
        except Exception:
            swallow.note('agent_scheduler_tasks._cron_field_match:L197')
            continue
        if value == 6 and v == 7:  # cron: 7 == воскресенье
            return True
        if v == value:
            return True
    return False


def is_cron_expression(s: str) -> bool:
    """Похоже на cron-выражение: ровно 5 полей из цифр/*,/-."""
    parts = str(s).strip().split()
    if len(parts) != 5:
        return False
    ok = all(re.match(r"^[\d*,/\-]+$", f) for f in parts)
    return ok and any(c.isdigit() or c == "*" for c in "".join(parts))


def cron_next_run(expr: str, after: float = None) -> Optional[float]:
    """Следующий timestamp по cron (мин час дом мес дн). dom+dow оба заданы —
    совпадение ЛЮБОГО (классика cron)."""
    try:
        fields = str(expr).strip().split()
        if len(fields) != 5 or not is_cron_expression(str(expr)):
            return None
        min_f, hour_f, dom_f, mon_f, dow_f = fields
        dom_r, dow_r = dom_f != "*", dow_f != "*"
        start = datetime.now().replace(second=0, microsecond=0)
        if after:
            start = max(start, datetime.fromtimestamp(after))
        t = start + timedelta(minutes=1)
        for _ in range(400000):
            if not _cron_field_match(min_f, t.minute):
                t += timedelta(minutes=1)
                continue
            if not _cron_field_match(hour_f, t.hour):
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if not _cron_field_match(mon_f, t.month):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            day_ok = _cron_field_match(dom_f, t.day)
            dow_ok = _cron_field_match(dow_f, t.weekday())
            if dom_r and dow_r:
                if not (day_ok or dow_ok):
                    t = t.replace(hour=0, minute=0) + timedelta(days=1)
                    continue
            else:
                if not (day_ok and dow_ok):
                    t = t.replace(hour=0, minute=0) + timedelta(days=1)
                    continue
            return t.timestamp()
        return None
    except Exception:
        swallow.note('agent_scheduler_tasks.cron_next_run:L251')
        return None


def parse_time_string(time_str: str) -> Optional[float]:
    """
    Parse time strings like:
    - "через 30 минут"
    - "через 2 часа"
    - "завтра в 10:00"
    - "15.04.2026 14:30"
    - "каждые 30 минут"
    - "каждый час"
    - "каждые 2 часа"
    """
    now = time.time()
    s = time_str.lower().strip()

    # "каждые N минут/часов"
    m = re.search(r'кажд(?:ые|ый|ую)\s+(\d+)\s*(минут|час|часов)', s)
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        minutes = num if 'минут' in unit else num * 60
        return now + (minutes * 60)  # First run now + interval

    # English: "in N minutes/hours/days"
    m = re.search(r'in\s+(\d+)\s*(minute|minutes|hour|hours|day|days)', s)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        mult = 60 if "minute" in unit else (3600 if "hour" in unit else 86400)
        return now + (num * mult)

    # English: "every N minutes/hours" → первый запуск через интервал
    m = re.search(r'every\s+(\d+)\s*(minute|minutes|hour|hours)', s)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        minutes = num if "minute" in unit else num * 60
        return now + (minutes * 60)

    # "через N минут"
    m = re.search(r'через\s+(\d+)\s*(минут|минуты|минуту|час|часа|часов|дней|день|дня)', s)
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        if 'минут' in unit or 'минуты' in unit or 'минуту' in unit:
            return now + (num * 60)
        elif 'час' in unit or 'часа' in unit or 'часов' in unit:
            return now + (num * 3600)
        elif 'дней' in unit or 'день' in unit or 'дня' in unit:
            return now + (num * 86400)

    # "завтра в HH:MM"
    m = re.search(r'завтра\s+в\s+(\d{1,2}):(\d{2})', s)
    if m:
        tomorrow = datetime.now() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                       int(m.group(1)), int(m.group(2))).timestamp()

    # "HH:MM" (today or tomorrow)
    m = re.search(r'(\d{1,2}):(\d{2})', s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = datetime.now().replace(hour=h, minute=mi, second=0)
        if target.timestamp() < now:
            target += timedelta(days=1)
        return target.timestamp()

    # "DD.MM.YYYY HH:MM"
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})', s)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                       int(m.group(4)), int(m.group(5))).timestamp()

    return None


def parse_interval(time_str: str) -> Optional[int]:
    """Parse interval in minutes from string (рус + англ)."""
    s = time_str.lower().strip()

    # English: "every N minutes/hours"
    m = re.search(r'every\s+(\d+)\s*(minute|minutes|hour|hours)', s)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        return num if "minute" in unit else num * 60

    # "every day/daily" без времени → 1440
    if re.search(r'\bevery day\b|\bdaily\b', s) and not re.search(r'at\s*\d{1,2}[:.]\d{2}', s):
        return 1440

    # "every hour/hourly"
    if re.search(r'\bevery hour\b|\bhourly\b', s):
        return 60

    # "каждые N дней/суток"
    m = re.search(r'кажд(?:ые|ый|ую)\s+(\d+)\s*(?:дн|суток)', s)
    if m:
        return int(m.group(1)) * 1440

    # "каждые N минут" / "каждую N минуту"
    m = re.search(r'кажд(?:ую|ые|ый)\s+(\d+)\s*минут', s)
    if m:
        return int(m.group(1))

    # "каждый N часов" / "каждые N часа"
    m = re.search(r'кажд(?:ые|ый|ую)\s+(\d+)\s*час', s)
    if m:
        return int(m.group(1)) * 60

    # "каждый день" / "каждые сутки"
    if "каждый день" in s or "каждые сутки" in s or "каждые 24 часа" in s:
        return 1440  # 24 hours

    # "каждый час"
    if "каждый час" in s or ("каждые" in s and "час" in s):
        return 60

    # "каждые 30 минут" (general)
    m = re.search(r'(\d+)\s*мин', s)
    if m:
        return int(m.group(1))

    m = re.search(r'(\d+)\s*час', s)
    if m:
        return int(m.group(1)) * 60

    return None


def create_scheduled_task(prompt: str, time_spec: str, model: str = None,
                        session_id: str = None) -> ScheduledTask:
    """
    Создать задачу из текстового описания.
    time_spec: "через 30 минут", "каждые 2 часа", "завтра в 10:00", etc.
    """
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    # Determine run type and time
    run_at = parse_time_string(time_spec)
    interval = parse_interval(time_spec)

    # "каждый день в 8:00" / "ежедневно в 08:00" / "каждое утро в 8:00" → daily
    daily_time = ""
    m_daily = re.search(r'(?:кажд(?:ый|ое)\s+(?:день|утро)|ежедневно|по\s+утрам)[^\d]{0,20}(\d{1,2}):(\d{2})',
                        time_spec.lower())
    if m_daily:
        hh, mm = int(m_daily.group(1)), int(m_daily.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            daily_time = f"{hh:02d}:{mm:02d}"
            target = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target.timestamp() <= time.time():
                target += timedelta(days=1)
            task = ScheduledTask(
                task_id=task_id, prompt=prompt, model=model,
                run_type="daily", run_at=target.timestamp(),
                interval_minutes=1440, max_runs=3650,
                session_id=session_id, daily_time=daily_time)
            save_task(task)
            logger.info(f"[SCHEDULED] Created DAILY: {task_id} — '{prompt[:50]}' at {daily_time}")
            return task

    if interval:
        # Interval task
        run_type = "interval"
        run_at = run_at or time.time()  # First run
    elif run_at:
        run_type = "at_time"
        interval = 0
    else:
        # Default: run in 5 minutes once
        run_type = "once"
        run_at = time.time() + 300
        interval = 0

    task = ScheduledTask(
        task_id=task_id,
        prompt=prompt,
        model=model,
        run_type=run_type,
        run_at=run_at,
        interval_minutes=interval,
        session_id=session_id
    )

    save_task(task)
    logger.info(f"[SCHEDULED] Created: {task_id} — '{prompt[:50]}' at {datetime.fromtimestamp(run_at).strftime('%Y-%m-%d %H:%M')}")
    return task


def _execute_task(task: ScheduledTask) -> Dict[str, Any]:
    """
    Выполнить запланированную задачу через agent chat-stream.
    """
    try:
        # Build request to agent's streaming endpoint
        import urllib.request
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        server_port = cfg.get("server", {}).get("port", 8080)

        # Log session info
        session_info = task.session_id if task.session_id else "NEW_SESSION"
        logger.info(f"[SCHEDULED] Task {task.id} will run with session: {session_info}")

        # Use agent's own streaming endpoint
        url = f"http://localhost:{server_port}/api/agent/chat-stream"
        session_to_use = task.session_id if task.session_id else ""
        payload = json.dumps({
            "session": session_to_use,
            "message": f"[SCHEDULED TASK] {task.prompt}",
            "model": task.model,
            "execMode": "autonomy",
            "maxIterations": 10
        }).encode()

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}
        )

        # Read streaming response
        result_text = ""
        action_results = []

        with urllib.request.urlopen(req, timeout=600) as resp:
            for line in resp:
                line = line.decode('utf-8').strip()
                if line.startswith('data: '):
                    try:
                        data = json.loads(line[6:])
                        if data.get('delta'):
                            result_text += data['delta']
                        if data.get('done'):
                            if data.get('full_answer'):
                                result_text = data['full_answer']
                            action_results = data.get('action_results', [])
                    except Exception:
                        swallow.note('agent_scheduler_tasks._execute_task:L491')
                        pass

        task.last_run = time.time()
        task.run_count += 1
        task.retry_count = 0
        task.last_result = {
            "answer": result_text[:5000],
            "actions": len(action_results),
            "timestamp": time.time()
        }
        task.execution_history.append({
            "ts": time.time(), "ok": True,
            "answer_preview": result_text[:300]
        })

        # Update next run for interval tasks
        if task.run_type == "interval":
            task.next_run = time.time() + (task.interval_minutes * 60)
            task.status = "pending"
        elif task.run_type == "daily" and task.daily_time:
            # Следующий запуск — завтра в то же время (без дрейфа)
            hh, mm = task.daily_time.split(":")[:2]
            nxt = datetime.now().replace(hour=int(hh), minute=int(mm),
                                         second=0, microsecond=0)
            if nxt.timestamp() <= time.time():
                nxt += timedelta(days=1)
            task.next_run = nxt.timestamp()
            task.status = "pending"
        else:
            task.status = "completed"

        save_task(task)
        logger.info(f"[SCHEDULED] Completed: {task.id} — {task.run_count}/{task.max_runs} runs")
        return {"success": True, "result": task.last_result}

    except Exception as e:
        task.last_result = {"error": str(e), "timestamp": time.time()}
        task.execution_history.append({"ts": time.time(), "ok": False, "error": str(e)[:300]})
        task.retry_count += 1
        if task.retry_count <= task.max_retries:
            # backoff: 1, 2, 4 минуты - задача остаётся pending и повторится
            backoff = 60 * (2 ** (task.retry_count - 1))
            task.next_run = time.time() + backoff
            task.status = "pending"
            logger.warning(
                f"[SCHEDULED] Retry {task.retry_count}/{task.max_retries} "
                f"for {task.id} in {backoff}s: {e}")
        else:
            task.status = "failed"
            logger.error(f"[SCHEDULED] Failed permanently: {task.id} - {e}")
        save_task(task)
        return {"success": False, "error": str(e), "retry": task.retry_count}


def _scheduler_loop():
    """Background thread that checks and runs scheduled tasks."""
    logger.info("[SCHEDULER] Started")
    while True:
        try:
            tasks = load_tasks()
            now = time.time()

            for task in tasks:
                # Skip non-pending tasks
                if task.status not in ("pending",):
                    continue

                # Skip future tasks
                if task.next_run and task.next_run > now:
                    continue

                # Check max runs
                if task.run_count >= task.max_runs:
                    task.status = "completed"
                    save_task(task)
                    continue

                # Execute
                logger.info(f"[SCHEDULER] Running: {task.id} — '{task.prompt[:50]}'")
                task.status = "running"
                save_task(task)

                # Run in separate thread to not block
                t = threading.Thread(target=_execute_task, args=(task,), daemon=True)
                t.start()

        except Exception as e:
            logger.error(f"[SCHEDULER] Error: {e}")

        time.sleep(30)  # Check every 30 seconds


def run_task_now(task_id: str) -> Dict[str, Any]:
    # Ручной запуск задачи по id (для API /run и отладки).
    tasks = load_tasks()
    task = next((t for t in tasks if t.id == task_id), None)
    if not task:
        return {"success": False, "error": "task not found"}
    task.next_run = time.time()
    task.status = "pending"
    save_task(task)
    return _execute_task(task)


def start_scheduler():
    """Start the background scheduler thread."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return {"status": "already_running"}

    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="TaskScheduler")
    _scheduler_thread.start()
    return {"status": "started"}


def get_tasks_summary() -> Dict[str, Any]:
    tasks = load_tasks()
    summary = {
        "total": len(tasks),
        "pending": sum(1 for t in tasks if t.status == "pending"),
        "running": sum(1 for t in tasks if t.status == "running"),
        "completed": sum(1 for t in tasks if t.status == "completed"),
        "failed": sum(1 for t in tasks if t.status == "failed"),
        "cancelled": sum(1 for t in tasks if t.status == "cancelled"),
    }
    return summary
