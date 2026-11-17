"""
Conversation Summarization Module
Авто-саммаризация длинных диалогов
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional, Any
import swallow

logger = logging.getLogger("infinity")

# Директория
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent")
SUMMARIES_DIR = os.path.join(AGENT_BASE, "summaries")
os.makedirs(SUMMARIES_DIR, exist_ok=True)


def summarize_conversation(
    messages: List[Dict[str, str]],
    max_summary_length: int = 500,
    call_lm_func: Optional[Any] = None,
    lm_studio_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Суммаризирует длинный диалог.
    
    Args:
        messages: Список сообщений
        max_summary_length: Максимальная длина саммаризации
        call_lm_func: Функция для вызова LLM
        lm_studio_url: URL LM Studio
        
    Returns:
        dict с саммаризацией
    """
    if len(messages) < 5:
        return {
            "success": False,
            "reason": "Too few messages to summarize",
            "message_count": len(messages)
        }
    
    # Формируем промпт для суммаризации
    conversation_text = "\n".join([
        f"{msg['role']}: {msg['content'][:200]}"
        for msg in messages[-20:]  # Берём последние 20 сообщений
    ])
    
    prompt = f"""Суммаризируй этот диалог в 3-5 предложениях. Выдели:
1. Основную тему/задачу
2. Ключевые решения
3. Результат

Диалог:
{conversation_text}

Суммаризация:"""
    
    # Если есть LLM функция - используем её
    if call_lm_func:
        try:
            summary = call_lm_func(
                [{"role": "user", "content": prompt}],
                lm_studio_url=lm_studio_url
            )
            
            result = {
                "success": True,
                "summary": summary[:max_summary_length],
                "original_messages": len(messages),
                "summary_length": len(summary),
                "compression_ratio": round(len(summary) / sum(len(m["content"]) for m in messages) * 100, 2),
                "timestamp": time.time()
            }
            
            # Сохраняем
            _save_summary(result)
            
            return result
        except Exception as e:
            logger.error(f"Summarization failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "fallback": True
            }
    
    # Fallback: простая экстрактивная суммаризация
    return _extractive_summary(messages, max_summary_length)


def _extractive_summary(messages: List[Dict[str, str]], max_length: int) -> Dict[str, Any]:
    """Простая экстрактивная суммаризация без LLM."""
    # Берём первое и последнее сообщение пользователя + ассистента
    key_messages = []
    
    if len(messages) >= 2:
        key_messages.append(messages[0])  # Первое
        key_messages.append(messages[-1])  # Последнее
    
    if len(messages) >= 4:
        mid = len(messages) // 2
        key_messages.insert(1, messages[mid])  # Среднее
    
    summary_text = " | ".join([
        f"{m['role']}: {m['content'][:150]}..."
        for m in key_messages
    ])
    
    result = {
        "success": True,
        "summary": summary_text[:max_length],
        "original_messages": len(messages),
        "summary_length": len(summary_text),
        "method": "extractive",
        "timestamp": time.time()
    }
    
    _save_summary(result)
    return result


def _save_summary(result: Dict[str, Any]):
    """Сохраняет саммаризацию."""
    summary_id = f"summary_{int(time.time())}.json"
    path = os.path.join(SUMMARIES_DIR, summary_id)
    
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def compress_session_messages(
    session: Dict[str, Any],
    max_messages: int = 10,
    call_lm_func: Optional[Any] = None,
    lm_studio_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Сжимает сообщения сессии для экономии контекстного окна.
    
    Args:
        session: Сессия с сообщениями
        max_messages: Максимум сообщений для отправки в LLM
        call_lm_func: Функция вызова LLM
        lm_studio_url: URL LM Studio
        
    Returns:
        Обновлённая сессия
    """
    messages = session.get("messages", [])
    
    if len(messages) <= max_messages:
        return session
    
    # Суммаризируем старые сообщения
    old_messages = messages[:-max_messages]
    recent_messages = messages[-max_messages:]
    
    summary_result = summarize_conversation(
        old_messages,
        call_lm_func=call_lm_func,
        lm_studio_url=lm_studio_url
    )
    
    if summary_result.get("success"):
        # Заменяем старые сообщения одним summary
        compressed_messages = [
            {"role": "system", "content": f"[SUMMARY OF PREVIOUS CONVERSATION]\n{summary_result['summary']}"},
            *recent_messages
        ]
        
        session["messages"] = compressed_messages
        session["summary_applied"] = True
        session["original_message_count"] = len(messages)
        session["compressed_message_count"] = len(compressed_messages)
        
        logger.info(f"[SUMMARY] Compressed {len(messages)} -> {len(compressed_messages)} messages")
    
    return session


def get_summaries_list() -> List[Dict[str, Any]]:
    """Получает список всех саммаризаций."""
    summaries = []
    
    for filename in os.listdir(SUMMARIES_DIR):
        if not filename.endswith(".json"):
            continue
        
        path = os.path.join(SUMMARIES_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            summaries.append({
                "id": filename.replace(".json", ""),
                "summary_length": data.get("summary_length", 0),
                "original_messages": data.get("original_messages", 0),
                "compression_ratio": data.get("compression_ratio", 0),
                "timestamp": data.get("timestamp", 0),
                "method": data.get("method", "llm")
            })
        except Exception:
            swallow.note('conversation_summary.get_summaries_list:L206')
            continue
    
    return sorted(summaries, key=lambda x: x.get("timestamp", 0), reverse=True)
