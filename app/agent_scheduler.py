"""
ASIO Agent Scheduler & Reasoning Engine v2.0
Планировщик задач с reasoning-итерациями между действиями агента.
Агент рассуждает между итерациями, решает что делать дальше.
"""

import os
import json
import time
import uuid
import re
import logging
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
import swallow

logger = logging.getLogger("infinity")

# Directories
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
SCHEDULER_DIR = os.path.join(AGENT_BASE, "scheduler")
REASONING_DIR = os.path.join(AGENT_BASE, "reasoning")
SKILLS_AUTO_DIR = os.path.join(AGENT_BASE, "auto_skills")
for d in [SCHEDULER_DIR, REASONING_DIR, SKILLS_AUTO_DIR]:
    os.makedirs(d, exist_ok=True)


class TaskStatus(Enum):
    PENDING = "pending"
    THINKING = "thinking"  # reasoning iteration
    PLANNING = "planning"  # building execution plan
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TaskPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class Task:
    """Представление задачи для планировщика."""

    def __init__(
        self,
        task_id: str,
        session_id: str,
        description: str,
        priority: str = "normal",
        parent_task_id: Optional[str] = None,
        profile_id: Optional[str] = None
    ):
        self.task_id = task_id
        self.session_id = session_id
        self.description = description
        self.status = TaskStatus.PENDING
        self.priority = TaskPriority(priority)
        self.parent_task_id = parent_task_id
        self.profile_id = profile_id or ""

        self.plan: List[Dict[str, Any]] = []  # steps
        self.current_step = 0
        self.reasoning_history: List[Dict[str, Any]] = []  # reasoning iterations
        self.execution_log: List[Dict[str, Any]] = []
        self.checkpoint: Dict[str, Any] = {}
        self.last_verification: Optional[Dict[str, Any]] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.error: Optional[str] = None
        self.metadata: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "parent_task_id": self.parent_task_id,
            "profile_id": self.profile_id,
            "plan": self.plan,
            "current_step": self.current_step,
            "reasoning_history": self.reasoning_history[-10:],  # last 10
            "execution_log": self.execution_log[-50:],
            "checkpoint": self.checkpoint,
            "last_verification": self.last_verification,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        task = cls(
            task_id=data["task_id"],
            session_id=data["session_id"],
            description=data["description"],
            priority=data.get("priority", "normal"),
            parent_task_id=data.get("parent_task_id"),
            profile_id=data.get("profile_id")
        )
        task.status = TaskStatus(data.get("status", "pending"))
        task.plan = data.get("plan", [])
        task.current_step = data.get("current_step", 0)
        task.reasoning_history = data.get("reasoning_history", [])
        task.execution_log = data.get("execution_log", [])
        task.checkpoint = data.get("checkpoint", {})
        task.last_verification = data.get("last_verification")
        task.created_at = data.get("created_at", time.time())
        task.started_at = data.get("started_at")
        task.completed_at = data.get("completed_at")
        task.error = data.get("error")
        task.metadata = data.get("metadata", {})
        return task


class Scheduler:
    """Планировщик задач агента с reasoning-итерациями."""

    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.active_session: Optional[str] = None
        self.cancel_flags: Dict[str, bool] = {}  # session_id -> cancel flag
        self._load_tasks()

    def _load_tasks(self):
        """Загружает задачи из файлов."""
        if not os.path.exists(SCHEDULER_DIR):
            return
        for fn in os.listdir(SCHEDULER_DIR):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(SCHEDULER_DIR, fn), encoding="utf-8") as f:
                        data = json.load(f)
                    task = Task.from_dict(data)
                    # Восстановить только незавершённые задачи
                    if task.status in (TaskStatus.PENDING, TaskStatus.EXECUTING, TaskStatus.THINKING):
                        if task.status == TaskStatus.EXECUTING:
                            task.status = TaskStatus.PENDING  # reset to re-plan
                        self.tasks[task.task_id] = task
                except Exception as e:
                    logger.error(f"Task load error: {e}")

    def _save_task(self, task: Task):
        """Атомарно сохраняет задачу, чтобы checkpoint не был потерян при сбое."""
        fp = os.path.join(SCHEDULER_DIR, f"{task.task_id}.json")
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, fp)

    def cancel_task(self, session_id: str):
        """Устанавливает флаг отмены для сессии."""
        self.cancel_flags[session_id] = True
        logger.info(f"[SCHEDULER] Cancel flag set for session {session_id}")

    def is_cancelled(self, session_id: str) -> bool:
        """Проверяет флаг отмены."""
        return self.cancel_flags.get(session_id, False)

    def clear_cancel(self, session_id: str):
        """Очищает флаг отмены."""
        self.cancel_flags.pop(session_id, None)

    def create_task(
        self,
        session_id: str,
        description: str,
        priority: str = "normal",
        parent_task_id: Optional[str] = None
    ) -> Task:
        """Создаёт новую задачу."""
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        task = Task(task_id, session_id, description, priority, parent_task_id, _current_profile_id())
        self.tasks[task_id] = task
        self._save_task(task)
        logger.info(f"[SCHEDULER] Task created: {task_id}, desc={description[:80]}")
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def get_session_tasks(self, session_id: str) -> List[Task]:
        return [t for t in self.tasks.values() if t.session_id == session_id]

    def get_resumable_task(self, session_id: str) -> Optional[Task]:
        """Находит незавершённую задачу сессии для продолжения после перезапуска."""
        candidates = [
            t for t in self.tasks.values()
            if t.session_id == session_id
            and t.status in (TaskStatus.PENDING, TaskStatus.THINKING, TaskStatus.EXECUTING, TaskStatus.PAUSED)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda t: t.created_at, reverse=True)
        task = candidates[0]
        # Задача была прервана посреди выполнения — вернуть в PENDING для безопасного продолжения
        if task.status in (TaskStatus.EXECUTING, TaskStatus.THINKING):
            task.status = TaskStatus.PENDING
            self._save_task(task)
        return task

    def list_tasks(self, status: Optional[str] = None, profile_id: Optional[str] = None) -> List[Dict[str, Any]]:
        tasks = list(self.tasks.values())
        if status:
            tasks = [t for t in tasks if t.status.value == status]
        if profile_id:
            tasks = [t for t in tasks if t.profile_id in (profile_id, "")]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in tasks[:50]]


def _current_profile_id() -> str:
    """ID активного профиля (пустая строка — общий/без профиля)."""
    try:
        import profiles_module as PM
        cur = PM.get_active_profile()
        return str(cur.get("id", "")) if cur else ""
    except Exception:
        swallow.note('agent_scheduler._current_profile_id:L229')
        return ""


# Global scheduler
_scheduler: Optional[Scheduler] = None

def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


def _truncate_text(text: Any, limit: int = 800) -> str:
    """Компактный текст для prompt/checkpoint, полный результат сохраняется отдельно."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return value[:limit] + f"... [truncated {omitted} chars]"


def _compact_tool_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Минимальная карточка результата для reasoning и состояния задачи."""
    return {
        "tool": result.get("tool"),
        "success": bool(result.get("success")),
        "output": _truncate_text(result.get("output", ""), 800),
        "error": _truncate_text(result.get("error", ""), 300),
        "timestamp": time.time(),
    }


def verify_tool_step(tool_call: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Verifier первого уровня: проверяет, что действие действительно выполнено."""
    verification = {
        "tool": tool_call.get("name"),
        "success": bool(result.get("success")),
        "checks": [],
        "verified": bool(result.get("success")),
        "message": "Инструмент завершился успешно" if result.get("success") else "Инструмент вернул ошибку",
        "timestamp": time.time(),
    }
    output = str(result.get("output", ""))
    if not result.get("success"):
        verification["checks"].append({"name": "tool_success", "ok": False})
        return verification
    if output.startswith("⛔") or "Traceback" in output:
        verification["verified"] = False
        verification["message"] = "Вывод похож на ошибку, несмотря на success=True"
    verification["checks"].append({"name": "output_error_scan", "ok": verification["verified"]})
    return verification


def _make_plan_from_message(message: str) -> List[Dict[str, Any]]:
    """Детерминированный стартовый план для слабой модели."""
    return [
        {
            "id": 1,
            "goal": _truncate_text(message, 300),
            "status": "pending",
            "tool": None,
            "args": {},
            "verification": {"required": True},
        }
    ]


def reasoning_iteration(
    task: Task,
    current_context: str,
    tool_results: List[Dict],
    call_llm_func: Callable,
    model: str,
    lm_studio_url: str
) -> Dict[str, Any]:
    """
    Reasoning iteration — агент рассуждает что делать дальше.
    Вызывается между действиями для анализа ситуации.
    """
    logger.info(f"[REASONING] Task {task.task_id}, step {task.current_step}")

    # Build reasoning prompt
    prompt = f"""Ты A.S.I.O. агент. Сейчас ты находишься в процессе выполнения задачи.

## ТЕКУЩАЯ ЗАДАЧА:
{task.description}

## ТЕКУЩИЙ СТАТУС:
- Шаг: {task.current_step} из {len(task.plan)}
- Статус: {task.status.value}

## КОНТЕКСТ:
{current_context}

## РЕЗУЛЬТАТЫ ДЕЙСТВИЙ:
"""
    for i, tr in enumerate(tool_results[-5:]):  # last 5 results
        prompt += f"{i+1}. [{tr.get('tool', '?')}] {'✅' if tr.get('success') else '❌'} {str(tr.get('output', ''))[:300]}\n"

    prompt += """
## РАССУДИ И ОТВЕТЬ В ФОРМАТЕ:

[THINKING]: Твои рассуждения — что произошло, что работает, что нет
[ANALYSIS]: Анализ текущей ситуации — что нужно изменить
[DECISION]: Твоё решение — что делать дальше
[PLAN_UPDATE]: Обновлённый план (если нужно) — список шагов
[NEXT_TOOL]: Какой инструмент использовать следующим и почему
[SELF_IMPROVE]: Нужно ли создать новый навык/инструмент для этой задачи? Если да — какой?

ВАЖНО:
- Рассуждай честно — если что-то не работает, скажи почему
- Решай autonomously — не спрашивай пользователя
- Если нужен новый инструмент — скажи какой и создай его
- Если задача выполнена — скажи DONE
"""

    messages = [
        {"role": "system", "content": "Ты автономный AI-агент. Рассуждай о своей задаче и решай что делать дальше."},
        {"role": "user", "content": prompt}
    ]

    try:
        reasoning_text = call_llm_func(
            messages,
            model=model,
            lm_studio_url=lm_studio_url,
            max_tokens=2048,
            temperature=0.5  # more deterministic for reasoning
        )

        # Save reasoning
        reasoning_entry = {
            "timestamp": time.time(),
            "step": task.current_step,
            "thinking": reasoning_text,
            "context_len": len(current_context)
        }
        task.reasoning_history.append(reasoning_entry)

        # Save to file
        reasoning_file = os.path.join(REASONING_DIR, f"{task.task_id}_{int(time.time())}.json")
        with open(reasoning_file, "w", encoding="utf-8") as f:
            json.dump(reasoning_entry, f, ensure_ascii=False, indent=2)

        logger.info(f"[REASONING] Saved to {reasoning_file}")
        return {"success": True, "reasoning": reasoning_text, "entry": reasoning_entry}

    except Exception as e:
        logger.error(f"[REASONING] Error: {e}")
        return {"success": False, "error": str(e)}


def auto_skill_creation(
    task: Task,
    observed_needs: str,
    call_llm_func: Callable,
    model: str,
    lm_studio_url: str
) -> Dict[str, Any]:
    """
    Автоматическое создание навыков/инструментов агентом.
    Агент анализирует что ему нужно и создаёт новый инструмент.
    """
    logger.info(f"[AUTO-SKILL] Analyzing needs for task {task.task_id}")

    prompt = f"""Ты A.S.I.O. агент. Ты можешь создавать для себя новые инструменты (skills).

## ТЕКУЩАЯ ЗАДАЧА:
{task.description}

## НАБЛЮДЕНИЯ И ПОТРЕБНОСТИ:
{observed_needs}

## СУЩЕСТВУЮЩИЕ ИНСТРУМЕНТЫ:
- file_read, file_write, file_edit, file_append, file_delete, file_list, file_mkdir
- web_search — поиск в интернете
- shell — команды shell
- code_exec — выполнение кода
- tool_create — создание инструментов
- package_install — установка пакетов
- memory_store, memory_recall — память
- subagent_spawn — суб-агенты

## ЗАДАНИЕ:
Проанализируй потребности и созда НОВЫЙ инструмент если:
1. Существующие инструменты не покрывают потребность
2. Есть повторяющаяся операция которую можно автоматизировать
3. Нужна специализированная функция для задачи

## ОТВЕТЬ В ФОРМАТЕ:

[NEED_ANALYSIS]: Анализ потребностей — что не хватает
[DECISION]: Создавать инструмент или нет (YES/NO)
[SKILL_NAME]: имя_инструмента.py
[SKILL_DESCRIPTION]: Описание что делает инструмент
[SKILL_CODE]: Полный Python код инструмента с функцией execute(args, folder)

Требования к коду инструмента:
- Должна быть функция: def execute(args: str, folder: str = None) -> dict
- Возвращает dict: {"success": bool, "output": str, ...}
- Обработка ошибок через try/except
- Документация в начале файла

Если создавать инструмент НЕ нужно — просто напиши [DECISION]: NO
"""

    messages = [
        {"role": "system", "content": "Ты автономный AI-агент. Создай полезный инструмент если он нужен."},
        {"role": "user", "content": prompt}
    ]

    try:
        result_text = call_llm_func(
            messages,
            model=model,
            lm_studio_url=lm_studio_url,
            max_tokens=4096,
            temperature=0.7
        )

        # Parse skill creation
        need_match = re.search(r'\[NEED_ANALYSIS\]:?\s*(.+?)(?=\n\[|$)', result_text, re.DOTALL)
        decision_match = re.search(r'\[DECISION\]:?\s*(YES|NO)', result_text, re.IGNORECASE)
        name_match = re.search(r'\[SKILL_NAME\]:?\s*(.+?)(?=\n|$)', result_text)
        desc_match = re.search(r'\[SKILL_DESCRIPTION\]:?\s*(.+?)(?=\n\[|$)', result_text, re.DOTALL)
        code_match = re.search(r'\[SKILL_CODE\]:?\s*(.+)', result_text, re.DOTALL)

        decision = decision_match.group(1).strip().upper() if decision_match else "NO"

        skill_result = {
            "success": True,
            "decision": decision,
            "need_analysis": need_match.group(1).strip() if need_match else "",
        }

        if decision == "YES" and name_match and code_match:
            skill_name = name_match.group(1).strip()
            if not skill_name.endswith(".py"):
                skill_name += ".py"

            skill_code = code_match.group(1).strip()
            # Remove code block markers
            skill_code = re.sub(r'^```(\w+)?\n', '', skill_code)
            skill_code = re.sub(r'\n```$', '', skill_code)

            # Save skill
            skill_path = os.path.join(SKILLS_AUTO_DIR, skill_name)
            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(skill_code)

            # Also register in tools dir
            from agent_module import TOOLS_DIR, register_tool
            tools_path = os.path.join(TOOLS_DIR, skill_name)
            with open(tools_path, "w", encoding="utf-8") as f:
                f.write(skill_code)

            description = desc_match.group(1).strip() if desc_match else skill_name

            register_tool(
                skill_name.replace(".py", ""),
                description,
                tools_path
            )

            skill_result["skill_name"] = skill_name
            skill_result["skill_path"] = skill_path
            skill_result["description"] = description
            logger.info(f"[AUTO-SKILL] Created: {skill_name}")

        return skill_result

    except Exception as e:
        logger.error(f"[AUTO-SKILL] Error: {e}")
        return {"success": False, "error": str(e)}


def fill_knowledge_graph(
    reasoning_text: str,
    task_description: str,
    tool_results: List[Dict]
) -> List[Dict[str, Any]]:
    """
    Автоматически заполняет граф знаний из результатов рассуждений.
    Извлекает факты, связи, концепции.
    """
    from memory_graph import get_memory_graph

    graph = get_memory_graph()
    created_nodes = []

    # Extract facts from reasoning
    sentences = re.split(r'[.!?]+', reasoning_text)

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 15 or len(sentence) > 500:
            continue

        # Skip meta-instructions
        if any(skip in sentence.lower() for skip in ['[thinking', '[analysis', '[decision', '[plan', '[tool:', '[skill']):
            continue

        # Create node
        node_id = f"kg_{abs(hash(sentence)) % 10000000}"
        node_type = "insight"

        # Determine type
        if "because" in sentence.lower() or "потому что" in sentence.lower():
            node_type = "causal"
        elif "should" in sentence.lower() or "нужно" in sentence.lower():
            node_type = "action_rule"
        elif "is" in sentence.lower() or "это" in sentence.lower() or "является" in sentence.lower():
            node_type = "fact"

        graph.add_node(node_id, sentence[:200], node_type, {
            "full_text": sentence,
            "source_task": task_description[:100],
            "source": "reasoning",
            "timestamp": time.time()
        })
        created_nodes.append(node_id)

    # Connect new nodes with existing similar ones
    for node_id in created_nodes:
        node = graph.nodes.get(node_id)
        if node:
            similar = graph.find_similar_nodes(node["label"], top_k=3)
            for sim_node in similar:
                if sim_node["id"] != node_id:
                    graph.add_edge(node_id, sim_node["id"], "related", 0.5)

    # Add tool usage nodes
    for tr in tool_results[-3:]:
        if tr.get("success"):
            tool_id = f"tool_{tr.get('tool', 'unknown')}_{int(time.time())}"
            graph.add_node(tool_id, f"Tool: {tr.get('tool', '?')}", "tool_usage", {
                "tool": tr.get("tool"),
                "output_preview": str(tr.get("output", ""))[:200],
                "task": task_description[:100],
                "timestamp": time.time()
            })
            created_nodes.append(tool_id)

    # Auto-connect similar nodes
    if len(created_nodes) > 2:
        graph.auto_connect_similar(threshold=0.3)

    logger.info(f"[KNOWLEDGE_GRAPH] Added {len(created_nodes)} nodes from reasoning")
    return [{"node_id": nid, "created": True} for nid in created_nodes]


def process_with_reasoning(
    session_id: str,
    message: str,
    call_llm_func: Callable,
    execute_tool_func: Callable,
    model: str,
    lm_studio_url: str,
    max_iterations: int = 10,
    reasoning_frequency: int = 2  # reasoning every N tool calls
) -> Dict[str, Any]:
    """
    Полный цикл обработки сообщения с reasoning-итерациями.
    Агент рассуждает между действиями, создаёт навыки, заполняет граф.
    """
    scheduler = get_scheduler()
    scheduler.clear_cancel(session_id)

    # Create task
    task = scheduler.create_task(session_id, message, "normal")
    task.status = TaskStatus.THINKING
    task.started_at = time.time()
    if not task.plan:
        task.plan = _make_plan_from_message(message)
    task.checkpoint = {"plan_initialized": True, "iteration": 0, "last_step_result": None}
    scheduler._save_task(task)

    # MEM_V2: журнал событий сессии
    try:
        import agent_memory as MEM
        MEM.record_event(session_id, "user", message)
    except Exception as _me:
        logger.debug(f"[MEM_V2] record user event failed: {_me}")

    # Load session
    from agent_module import load_session, _save_session, get_memory_context, build_system_prompt
    session = load_session(session_id)
    if not session:
        return {"error": "Сессия не найдена"}

    folder = session.get("folder")
    memory_ctx = get_memory_context()

    # Build system prompt
    system_prompt = build_system_prompt(
        mode=session.get("mode", "general"),
        folder=folder,
        memory_context=memory_ctx,
        rag_context="",
        team_context="",
        available_tools=""
    )

    all_tool_results = []
    full_answer = ""
    reasoning_count = 0
    skill_count = 0
    graph_nodes = []

    try:
        for iteration in range(max_iterations):
            # Check cancel
            if scheduler.is_cancelled(session_id):
                task.status = TaskStatus.CANCELLED
                task.completed_at = time.time()
                scheduler._save_task(task)
                scheduler.clear_cancel(session_id)
                return {
                    "success": True,
                    "cancelled": True,
                    "answer": full_answer or "Задача отменена пользователем.",
                    "actions_executed": all_tool_results,
                    "reasoning_iterations": reasoning_count,
                    "skills_created": skill_count,
                    "graph_nodes_created": len(graph_nodes),
                    "iterations": iteration
                }

            # === REASONING PHASE (every N iterations) ===
            if iteration > 0 and iteration % reasoning_frequency == 0:
                task.status = TaskStatus.THINKING
                scheduler._save_task(task)

                reasoning_result = reasoning_iteration(
                    task, system_prompt, all_tool_results,
                    call_llm_func, model, lm_studio_url
                )

                if reasoning_result.get("success"):
                    reasoning_count += 1
                    reasoning_text = reasoning_result["reasoning"]

                    # Fill knowledge graph from reasoning
                    graph_nodes.extend(
                        fill_knowledge_graph(reasoning_text, message, all_tool_results)
                    )

                    # Check if task is done
                    if "DONE" in reasoning_text.upper() or "ЗАДАЧА ВЫПОЛНЕНА" in reasoning_text.upper():
                        task.status = TaskStatus.COMPLETED
                        task.completed_at = time.time()
                        scheduler._save_task(task)
                        break

                    # Auto-skill creation (every 3rd reasoning)
                    if reasoning_count % 3 == 0:
                        skill_result = auto_skill_creation(
                            task, reasoning_text,
                            call_llm_func, model, lm_studio_url
                        )
                        if skill_result.get("decision") == "YES":
                            skill_count += 1

            # === EXECUTION PHASE ===
            task.status = TaskStatus.EXECUTING
            scheduler._save_task(task)

            # Build messages — компактный контекст для слабой локальной модели
            lm_messages = [{"role": "system", "content": system_prompt}]
            recent = session.get("messages", [])[-10:]
            lm_messages.extend([{"role": m["role"], "content": m["content"]} for m in recent])
            if not recent or recent[-1].get("content") != message:
                lm_messages.append({"role": "user", "content": message})

            # Call LLM — низкая температура и лимит токенов для детерминизма
            try:
                answer = call_llm_func(
                    lm_messages,
                    model=model,
                    lm_studio_url=lm_studio_url,
                    max_tokens=2048,
                    temperature=0.2
                )
            except Exception as e:
                logger.error(f"[SCHEDULER] LLM call error: {e}")
                if iteration == 0:
                    # First iteration failed — return error
                    task.status = TaskStatus.FAILED
                    task.error = str(e)
                    task.completed_at = time.time()
                    scheduler._save_task(task)
                    return {"success": False, "error": str(e)}
                continue

            # Parse tool calls
            from agent_tools import parse_tool_calls
            try:
                from agent_tools import dedup_tool_calls as _dedup
            except Exception:
                swallow.note('agent_scheduler.process_with_reasoning:L730')
                _dedup = None
            tool_calls = parse_tool_calls(answer)
            if _dedup and tool_calls:
                tool_calls = _dedup(tool_calls)

            if not tool_calls:
                # No tools — agent finished thinking
                full_answer = answer
                session["messages"].append({"role": "assistant", "content": answer, "time": time.time()})
                _save_session(session)
                try:
                    import agent_memory as MEM
                    MEM.record_event(session_id, "assistant", answer)
                    facts = MEM.extract_facts_from_text(message, source=f"session:{session_id}")
                    if facts:
                        MEM.store_facts(facts)
                    MEM.auto_summarize_session(session_id, call_llm_func=call_llm_func,
                                               model=model, lm_studio_url=lm_studio_url)
                except Exception as _me:
                    logger.debug(f"[MEM_V2] finalize hooks failed: {_me}")
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                scheduler._save_task(task)
                break

            # Этап 1: максимум один tool call за итерацию — слабая модель
            # надёжнее выполняет действия по одному, с проверкой между ними.
            if len(tool_calls) > 1:
                logger.info(f"[SCHEDULER] {len(tool_calls)} tool calls, берём только первый")
            tool_calls = tool_calls[:1]

            # Execute tools
            for tc in tool_calls:
                # Check cancel
                if scheduler.is_cancelled(session_id):
                    task.status = TaskStatus.CANCELLED
                    task.completed_at = time.time()
                    scheduler._save_task(task)
                    scheduler.clear_cancel(session_id)
                    return {
                        "success": True,
                        "cancelled": True,
                        "answer": full_answer or "Задача отменена.",
                        "actions_executed": all_tool_results,
                        "reasoning_iterations": reasoning_count,
                        "skills_created": skill_count,
                        "graph_nodes_created": len(graph_nodes),
                        "iterations": iteration
                    }

                try:
                    import agent_memory as MEM
                    MEM.record_event(session_id, "tool_call", tc["args"],
                                     {"tool": tc["name"], "iteration": iteration})
                except Exception:
                    swallow.note('agent_scheduler.process_with_reasoning:L786')
                    pass
                result = execute_tool_func(tc["name"], tc["args"], session, folder, lm_studio_url)
                all_tool_results.append(result)
                try:
                    import agent_memory as MEM
                    MEM.record_event(session_id, "tool_result",
                                     _truncate_text(result.get("output", ""), 1500),
                                     {"tool": tc["name"], "success": bool(result.get("success"))})
                except Exception:
                    swallow.note('agent_scheduler.process_with_reasoning:L796')
                    pass

                # Verifier: проверка результата шага до перехода к следующему
                verification = verify_tool_step(tc, result)
                task.last_verification = verification

                # Add to session — компактная версия, полный результат в execution_log
                session["messages"].append({
                    "role": "assistant",
                    "content": f"[TOOL:{tc['name']}] {_truncate_text(tc['args'], 300)}"
                })
                session["messages"].append({
                    "role": "user",
                    "content": f"Результат [{tc['name']}]: {_truncate_text(result.get('output', ''), 1000)}"
                })
                _save_session(session)

                task.current_step += 1
                task.execution_log.append({
                    "tool": tc["name"],
                    "args": _truncate_text(tc["args"], 500),
                    "success": result.get("success", False),
                    "verified": verification.get("verified", False),
                    "output": _truncate_text(result.get("output", ""), 1500),
                    "time": time.time()
                })
                # Checkpoint: состояние сохраняется после КАЖДОГО шага,
                # чтобы задача могла продолжиться после перезапуска.
                task.checkpoint = {
                    "iteration": iteration,
                    "last_tool": tc["name"],
                    "last_step_result": _compact_tool_result(result),
                    "last_verification": verification,
                    "steps_completed": task.current_step,
                    "saved_at": time.time(),
                }
                scheduler._save_task(task)

        # Finalize
        if not full_answer:
            full_answer = f"Задача обработана за {iteration + 1} итераций.\n\n"
            if all_tool_results:
                full_answer += "Выполненные действия:\n"
                for i, r in enumerate(all_tool_results[-10:], 1):
                    status = "✅" if r.get("success") else "❌"
                    full_answer += f"{i}. {status} [{r.get('tool', '?')}] {str(r.get('output', ''))[:200]}\n"

        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        scheduler._save_task(task)

        return {
            "success": True,
            "answer": full_answer,
            "actions_executed": all_tool_results,
            "reasoning_iterations": reasoning_count,
            "skills_created": skill_count,
            "graph_nodes_created": len(graph_nodes),
            "iterations": max_iterations,
            "task_id": task.task_id
        }

    except Exception as e:
        import traceback
        task.status = TaskStatus.FAILED
        task.error = str(e)
        task.completed_at = time.time()
        scheduler._save_task(task)

        logger.error(f"[SCHEDULER] Fatal error: {e}")
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()[:1000],
            "actions_executed": all_tool_results,
            "task_id": task.task_id
        }
