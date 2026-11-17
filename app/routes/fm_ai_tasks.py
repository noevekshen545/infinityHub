"""
Задания AI-агента (cron) для файлового менеджера.

Тонкая обёртка над кроном Infinity Agent: пользователь пишет задание словами
(«каждый день в 14:00 собери все текстовые файлы из D:\\Docs в D:\\Archive»),
мы превращаем это в cron-задачу агента. Сам агент и его планировщик не трогаем —
единственный источник правды остаётся tools.cronjob_tools.cronjob, а мы ходим
в него через /api/cron/* web-UI агента (127.0.0.1:9120), тем же путём, каким
пользуется его собственный интерфейс.

Формат ответа наружу всегда одинаковый: {"ok": bool, ...}. Фронт не знает
ни про формат агента, ни про то, что там прокси и два процесса.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import swallow

logger = logging.getLogger("infinity")

router = APIRouter(prefix="/api/fm-ai-tasks", tags=["FM AI Tasks"])

# Агент может писать в консоль задачи — это его штатное поведение.
_REQUEST_TIMEOUT = 30.0
# Подсказки по расписанию показываем прямо в форме: пользователь не обязан
# знать синтаксис cron, агент понимает и человеческий.
SCHEDULE_HINTS = [
    "every day at 2pm — каждый день в 14:00",
    "every monday 9am — каждый понедельник в 09:00",
    "weekdays at 8am — по будням в 08:00",
    "every 2h — каждые 2 часа",
    "30m — каждые 30 минут",
    "in 20m — один раз через 20 минут",
    "0 9 * * * — точное расписание cron",
]


def _hermes():
    """Модуль интеграции агента или None, если он не подключён."""
    try:
        from hermes_module.integration import launch
        return launch
    except Exception as e:
        logger.warning(f"fm_ai_tasks: модуль агента недоступен: {e}")
        return None


async def _agent_http(method: str, path: str,
                      params: Optional[Dict[str, str]] = None,
                      payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Один HTTP-запрос к web-UI агента. Всегда возвращает словарь, исключений не рвёт.

    Фронт агента (UI_PORT) сам проксирует /api/sessions/* и прочие пути на шлюз,
    поэтому ходим только в него — тот же путь, что у собственного интерфейса агента.
    """
    launch = _hermes()
    if launch is None:
        return {"ok": False, "error": "Модуль Infinity Agent недоступен"}

    try:
        import httpx
        from hermes_module.integration import config as hf_config
    except Exception as e:
        swallow.note('fm_ai_tasks._agent_http:L68', e)
        return {"ok": False, "error": f"Не удалось подготовить запрос к агенту: {e}"}

    try:
        # Агент поднимается лениво, как при первом заходе на /agentai.
        launch.start_async_if_needed()
    except Exception as e:
        logger.warning(f"fm_ai_tasks: не удалось поднять агента: {e}")

    base = f"http://127.0.0.1:{hf_config.UI_PORT}"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.request(method, f"{base}{path}",
                                        params=params or {}, json=payload)
    except Exception as e:
        logger.warning(f"fm_ai_tasks: {method} {path} failed: {e}")
        return {"ok": False,
                "error": "Агент не отвечает. Откройте Infinity Agent и дождитесь запуска."}

    try:
        data = resp.json()
    except Exception:
        swallow.note('fm_ai_tasks._agent_http:L90')
        return {"ok": False, "error": f"Агент вернул не-JSON (код {resp.status_code})"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "Неожиданный ответ агента"}
    if data.get("success") is False or (resp.status_code >= 400 and not data.get("ok")):
        return {"ok": False,
                "error": data.get("error") or data.get("detail") or "Агент отклонил запрос",
                "detail": data}
    return {"ok": True, "raw": data}


async def _cron(action: str, payload: Optional[Dict[str, Any]] = None,
                params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Один вызов крона агента.

    Список берём с GET /api/cron/jobs, остальные действия — POST /api/cron/{action}:
    в web-UI агента это разные обработчики, и GET на /api/cron/list не существует.
    """
    if action == "list":
        return await _agent_http("GET", "/api/cron/jobs", params=params)
    return await _agent_http("POST", f"/api/cron/{action}", payload=payload or {})


def _job_view(job: Dict[str, Any]) -> Dict[str, Any]:
    """Привести задачу к виду, который ожидает форма (поля от _format_job)."""
    if not isinstance(job, dict):
        return {}
    job_id = str(job.get("job_id") or job.get("id") or "")
    return {
        "id": job_id,
        "name": job.get("name") or job_id,
        "prompt": job.get("prompt_preview") or "",
        "prompt_preview": job.get("prompt_preview") or "",
        "schedule": job.get("schedule") or "",
        "repeat": job.get("repeat") or "",
        "deliver": job.get("deliver") or "local",
        "enabled": bool(job.get("enabled", True)),
        "state": job.get("state") or "scheduled",
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "last_delivery_error": job.get("last_delivery_error"),
        "skills": job.get("skills") or [],
        "model": job.get("model"),
        "workdir": job.get("workdir") or "",
        "pinned": bool(job.get("pinned")),
    }


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        swallow.note('fm_ai_tasks._body:L145')
        return {}
    return body if isinstance(body, dict) else {}


# ─────────────────────────── CRUD ─────────────────────────────────────

@router.get("")
@router.get("/")
async def list_tasks(include_disabled: bool = True):
    """Список заданий агента."""
    params = {"include_disabled": "true" if include_disabled else "false"}
    res = await _cron("list", params=params)
    if not res.get("ok"):
        return res
    jobs_raw = res["raw"].get("jobs") or []
    return {
        "ok": True,
        "jobs": [_job_view(j) for j in jobs_raw if isinstance(j, dict)],
        "gateway_running": res["raw"].get("gateway_running"),
        "schedule_hints": SCHEDULE_HINTS,
    }


def _clean_workdir(raw: Any) -> Optional[str]:
    """Путь папки-контекста: принимаем только существующую абсолютную директорию.

    Крон-инструмент агента сам требует «absolute existing path» (workdir), и
    молча игнорировать битый путь честнее, чем создать задание с привязкой
    в никуда: пользователь увидит, что контекст не прикрепился.
    """
    path = str(raw or "").strip().strip('"')
    if not path:
        return ""
    if not os.path.isabs(path) or not os.path.isdir(path):
        return None
    return path


@router.post("")
@router.post("/")
async def create_task(request: Request):
    """Создать задание: {name, prompt, schedule, workdir?, run_now?}."""
    body = await _body(request)
    name = str(body.get("name") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    schedule = str(body.get("schedule") or "").strip()

    if not prompt:
        return {"ok": False, "error": "Опишите, что делать агенту"}
    if not schedule:
        return {"ok": False, "error": "Укажите расписание (например: every day at 2pm)"}

    payload: Dict[str, Any] = {
        "prompt": prompt,
        "schedule": schedule,
        "deliver": str(body.get("deliver") or "local"),
    }
    if "workdir" in body:
        workdir = _clean_workdir(body.get("workdir"))
        if workdir is None:
            return {"ok": False, "error": "Папка контекста не существует"}
        if workdir:
            # workdir — штатный механизм агента: в контекст подмешиваются
            # AGENTS.md/контекст-файлы папки, а файловые инструменты стартуют в ней.
            payload["workdir"] = workdir
    if name:
        payload["name"] = name
    skills = body.get("skills")
    if isinstance(skills, list) and skills:
        payload["skills"] = [str(s) for s in skills]

    res = await _cron("create", payload)
    if not res.get("ok"):
        return res

    data = res["raw"]
    job = _job_view(data.get("job") or {})
    job_id = str(data.get("job_id") or job.get("id") or "")
    if not job_id:
        return {"ok": False, "error": "Агент создал задание, но не вернул его номер"}

    # «Выполнить сразу» — отдельный вызов: агент запускает задание в фоне и
    # возвращает управление сразу, ждать результата не нужно.
    if body.get("run_now"):
        run = await _cron("run", {"job_id": job_id})
        job["started"] = bool(run.get("ok"))
        if not run.get("ok"):
            job["run_error"] = run.get("error")

    return {"ok": True, "job": job, "message": data.get("message") or ""}


@router.put("/{job_id}")
@router.patch("/{job_id}")
async def update_task(job_id: str, request: Request):
    """Изменить задание: любое подмножество {name, prompt, schedule, deliver}."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return {"ok": False, "error": "Не указан номер задания"}
    body = await _body(request)

    updates: Dict[str, Any] = {}
    for key in ("name", "prompt", "schedule", "deliver"):
        val = body.get(key)
        if val is not None and str(val).strip():
            updates[key] = str(val).strip()
    if "workdir" in body:
        workdir = _clean_workdir(body.get("workdir"))
        if workdir is None:
            return {"ok": False, "error": "Папка контекста не существует"}
        # workdir — штатный механизм агента: в контекст подмешиваются
        # AGENTS.md/контекст-файлы папки, а файловые инструменты стартуют в ней.
        updates["workdir"] = workdir
    if not updates:
        return {"ok": False, "error": "Нет изменений"}

    res = await _cron("update", dict(updates, job_id=job_id))
    if not res.get("ok"):
        return res
    return {"ok": True, "job": _job_view(res["raw"].get("job") or {})}


async def _simple_action(job_id: str, action: str) -> Dict[str, Any]:
    job_id = str(job_id or "").strip()
    if not job_id:
        return {"ok": False, "error": "Не указан номер задания"}
    res = await _cron(action, {"job_id": job_id})
    if not res.get("ok"):
        return res
    out: Dict[str, Any] = {"ok": True}
    if action != "remove" and isinstance(res["raw"].get("job"), dict):
        out["job"] = _job_view(res["raw"]["job"])
    return out


@router.post("/{job_id}/pause")
async def pause_task(job_id: str):
    """Поставить задание на паузу."""
    return await _simple_action(job_id, "pause")


@router.post("/{job_id}/resume")
async def resume_task(job_id: str):
    """Снять задание с паузы."""
    return await _simple_action(job_id, "resume")


@router.post("/{job_id}/run")
async def run_task(job_id: str):
    """Выполнить задание сейчас, не дожидаясь расписания."""
    return await _simple_action(job_id, "run")


@router.delete("/{job_id}")
async def delete_task(job_id: str):
    """Удалить задание."""
    return await _simple_action(job_id, "remove")


# ─────────────────── Вывод агента (сессии выполнений) ───────────────────
# Каждое выполнение задания — отдельная сессия агента cron_{job_id}_{timestamp}
# (cron/scheduler.run_job). Её transcript читается через
# GET /api/sessions/{sid}/messages, а ответить агенту в ту же сессию можно через
# POST /api/sessions/{sid}/chat. Обе точки уже проксирует web-UI агента на шлюз.

def _run_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": str(row.get("id") or ""),
        "title": row.get("title") or "",
        "started_at": row.get("started_at"),
        "last_active": row.get("last_active"),
        "message_count": row.get("message_count") or 0,
        "is_active": bool(row.get("is_active")),
    }


def _ts_num(val: Any) -> float:
    """Порядковый ключ времени: epoch (float/int) или ISO-строка — что отдаст стор."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and val.strip():
        try:
            return float(val)
        except ValueError:
            swallow.note('fm_ai_tasks._ts_num:L330')
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(val.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            swallow.note('fm_ai_tasks._ts_num:L336')
            return 0.0
    return 0.0


async def _job_runs(job_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Запуски задания — cron-сессии с id-префиксом задания, новые сначала."""
    res = await _agent_http("GET", "/api/sessions",
                            params={"source": "cron", "limit": "200"})
    if not res.get("ok"):
        return []
    prefix = f"cron_{job_id}_"
    rows = [r for r in (res["raw"].get("data") or [])
            if isinstance(r, dict) and str(r.get("id") or "").startswith(prefix)]
    rows.sort(key=lambda r: _ts_num(r.get("started_at")), reverse=True)
    return [_run_view(r) for r in rows[:max(1, min(limit, 100))]]


def _msg_text(msg: Dict[str, Any]) -> str:
    """Текст сообщения transcript'а: display-проекция важнее сырого content;
    list-контент (text-чанки) склеиваем."""
    for key in ("display_content", "content"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list):
            parts = [str(p.get("text") or "") for p in val
                     if isinstance(p, dict) and p.get("type") == "text"]
            joined = "\n".join(x for x in parts if x.strip())
            if joined:
                return joined
    return ""


@router.get("/{job_id}/runs")
async def task_runs(job_id: str, limit: int = 20):
    """История запусков задания (сессий) — для выбора, чей вывод показывать."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return {"ok": False, "error": "Не указан номер задания"}
    runs = await _job_runs(job_id, limit)
    if not runs:
        # Пусто — не ошибка: у задания просто ещё не было выполнений.
        return {"ok": True, "runs": []}
    return {"ok": True, "runs": runs}


@router.get("/{job_id}/output")
async def task_output(job_id: str, session_id: str = ""):
    """Что ответил агент в конкретном запуске (или в последнем)."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return {"ok": False, "error": "Не указан номер задания"}
    sid = str(session_id or "").strip()
    if not sid:
        runs = await _job_runs(job_id, 1)
        if not runs:
            return {"ok": False, "error": "Задание ещё ни разу не выполнялось"}
        sid = runs[0]["session_id"]
    res = await _agent_http("GET", f"/api/sessions/{sid}/messages",
                            params={"order": "oldest", "limit": "200"})
    if not res.get("ok"):
        return res
    # Шлюз отдаёт список в ключе data (OpenAI-подобная форма), дашборд — в messages.
    raw = res["raw"]
    messages = []
    for m in raw.get("data") or raw.get("messages") or []:
        if not isinstance(m, dict) or m.get("display_kind") == "hidden":
            continue
        role = str(m.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = _msg_text(m)
        if text:
            messages.append({"role": role, "text": text, "ts": m.get("timestamp")})
    return {"ok": True, "session_id": sid, "messages": messages}


@router.post("/{job_id}/chat")
async def task_chat(job_id: str, request: Request):
    """Ответить агенту в сессию конкретного выполнения задания."""
    body = await _body(request)
    sid = str(body.get("session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sid:
        # Без явного запуска отвечаем в последний — «написать агенту» с плашки.
        job_id = str(job_id or "").strip()
        runs = await _job_runs(job_id, 1)
        if not runs:
            return {"ok": False, "error": "Задание ещё ни разу не выполнялось"}
        sid = runs[0]["session_id"]
    if not message:
        return {"ok": False, "error": "Пустое сообщение"}

    res = await _agent_http("POST", f"/api/sessions/{sid}/chat",
                            payload={"message": message})
    if not res.get("ok"):
        return res
    data = res["raw"]
    msg = data.get("message") or {}
    reply = _msg_text(msg) if isinstance(msg, dict) else ""
    return {"ok": True,
            "session_id": str(data.get("session_id") or sid),
            "reply": reply or "Агент ответил пусто."}


@router.get("/status")
async def agent_status():
    """Жив ли агент — чтобы форма не предлагала создать задание вслепую."""
    launch = _hermes()
    if launch is None:
        return {"ok": False, "error": "Модуль Infinity Agent недоступен"}
    try:
        # launch.status() → health() запускает netstat/powershell с таймаутами
        # до 8/25 с. В обработчике это замораживает event loop целиком.
        state = await asyncio.to_thread(launch.status)
    except Exception as e:
        logger.debug(f"fm_ai_tasks: launch.status failed: {e}")
        state = None
    return {"ok": True, "state": state, "schedule_hints": SCHEDULE_HINTS}
