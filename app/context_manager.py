"""
Context Compression & Retry Module
Автоматическое сжатие контекста и retry при ошибках
"""

import re
import time
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("infinity")


def extract_structured_state(messages):
    """Сохраняет важные данные при сжатии контекста."""
    state={"goal":"","files":[],"tools":{},"errors":[],"decisions":[],"questions":[]}
    for m in messages:
        text=str(m.get("content","") or "")
        role=m.get("role","")
        if role=="user" and not state["goal"] and not text.startswith("Результат ["):
            state["goal"]=text[:300]
        for name in re.findall(r"\[TOOL:([A-Za-z_]\w*)\]",text):
            state["tools"][name]=state["tools"].get(name,0)+1
        for path in re.findall(r"[\w./\\-]+\.(?:py|js|ts|html|css|json|md|txt|yml|yaml|toml)",text):
            if path not in state["files"]: state["files"].append(path)
        if re.search(r"error|exception|traceback|failed|ошибка|не удалось|❌|⛔",text,re.I):
            state["errors"].extend(x.strip()[:180] for x in text.splitlines() if re.search(r"error|exception|traceback|failed|ошибка|❌|⛔",x,re.I))
        if role=="assistant":
            for line in text.splitlines():
                line=line.strip()
                if line.lower().startswith(("решение:","decision:","итог:","вывод:")): state["decisions"].append(line[:200])
                if line.endswith("?") and len(line)<160: state["questions"].append(line)
    state["files"]=state["files"][:25]; state["errors"]=state["errors"][-10:]; state["decisions"]=state["decisions"][-8:]; state["questions"]=state["questions"][-5:]
    return state

def format_structured_state(state):
    out=["=== TASK STATE ==="]
    if state["goal"]: out.append("GOAL: "+state["goal"])
    if state["files"]: out.append("FILES: "+", ".join(state["files"][:15]))
    if state["tools"]: out.append("TOOLS: "+", ".join(k+"x"+str(v) for k,v in state["tools"].items()))
    if state["decisions"]: out.append("DECISIONS:\n"+"\n".join("- "+x for x in state["decisions"]))
    if state["errors"]: out.append("ERRORS:\n"+"\n".join("- "+x for x in state["errors"]))
    return "\n".join(out)

def compress_context(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Сжимает контекст: суммаризует старые сообщения, оставляет последние."""
    if len(messages) <= 3:
        return messages

    # Оставляем system message + последние 5 сообщений
    system_msg = messages[0] if messages[0].get("role") == "system" else None
    recent = messages[-5:]

    # Суммаризируем старые сообщения
    old_messages = messages[1:-5] if system_msg else messages[:-5]
    if old_messages:
        summary_text = format_structured_state(extract_structured_state(messages)) + "\n\n=== HISTORY ===\n"
        for m in old_messages[-10:]:  # Берём максимум 10 старых
            content = m.get("content", "")[:200]
            summary_text += f"[{m['role']}]: {content}\n"
        summary_msg = {"role": "user", "content": summary_text}

        if system_msg:
            return [system_msg, summary_msg] + recent
        return [summary_msg] + recent

    if system_msg:
        return [system_msg] + recent
    return recent


def compress_context_aggressive(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Агрессивное сжатие: оставляем только system + последние 3 сообщения."""
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    recent = messages[-3:]

    # Создаём краткую сводку
    old_count = len(messages) - len(recent) - (1 if system_msg else 0)
    if old_count > 0:
        summary = {"role": "user", "content": f"[{old_count} previous messages were summarized to save context]"}
        if system_msg:
            return [system_msg, summary] + recent
        return [summary] + recent

    if system_msg:
        return [system_msg] + recent
    return recent


def should_compress(messages: List[Dict[str, str]], max_tokens: int) -> bool:
    """Проверяет нужно ли сжать контекст."""
    total_chars = sum(len(m.get("content", "")) for m in messages)
    max_context_chars = max_tokens * 4  # ~4 символа на токен
    return total_chars > max_context_chars * 0.8


def _is_server_error(error) -> bool:
    """HTTP 5xx / "Internal Server Error" от llama-server — временные ошибки
    (гонки слотов, перезагрузка движка). Стоит повторить с задержкой."""
    s = str(error).lower()
    return ("500" in s and ("http" in s or "error" in s or "code" in s)) or \
           "internal server error" in s or "bad gateway" in s or "service unavailable" in s


def call_with_retry(llm_call_func, max_retries: int = 3, **kwargs) -> str:
    """
    Вызывает LLM с автоматическим retry при ошибках.
    
    Args:
        llm_call_func: Функция вызова LLM (принимает messages и kwargs)
        max_retries: Максимум попыток
        **kwargs: Аргументы для функции
    
    Returns:
        Ответ LLM
    """
    last_error = None
    messages = kwargs.get("messages", [])

    for attempt in range(max_retries):
        try:
            return llm_call_func(**kwargs)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            logger.warning(f"[LLM_RETRY] Attempt {attempt+1}/{max_retries} failed: {e}")

            # Если контекст переполнен — сжимаем
            if "context" in error_str and ("length" in error_str or "exceed" in error_str or "limit" in error_str):
                logger.info("[LLM_RETRY] Context overflow, compressing...")
                if attempt == 0:
                    kwargs["messages"] = compress_context(messages)
                else:
                    kwargs["messages"] = compress_context_aggressive(messages)
                time.sleep(1 * (attempt + 1))
                continue

            # Если таймаут — пробуем с меньшими токенами
            if "timeout" in error_str or "timed out" in error_str:
                logger.info("[LLM_RETRY] Timeout, reducing max_tokens...")
                if "max_tokens" in kwargs:
                    kwargs["max_tokens"] = max(kwargs["max_tokens"] // 2, 1024)
                time.sleep(1 * (attempt + 1))
                continue

            # HTTP 5xx от llama-server (занятый слот/перезапуск движка):
            # сжатие контекста не поможет — ждём и повторяем тот же запрос.
            if _is_server_error(e):
                wait = 2.0 * (attempt + 1)
                logger.info(f"[LLM_RETRY] HTTP 5xx от движка, повтор через {wait:.0f}с...")
                time.sleep(wait)
                continue

            # Другие ошибки — просто retry
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
                continue

    raise last_error
