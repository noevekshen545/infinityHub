"""
Prompt Engineer Module
Отдельная модель для улучшения промтов перед отправкой агенту.
Работает в режиме: пользователь → prompt engineer → основной агент
"""

import os
import json
import time
from typing import Dict, Optional, Any
import swallow

# Directories
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
PROMPT_ENG_DIR = os.path.join(AGENT_BASE, "prompt_engineer")
os.makedirs(PROMPT_ENG_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(PROMPT_ENG_DIR, "config.json")

DEFAULT_CONFIG = {
    "enabled": False,
    "model": "gemma-3-4b-it",
    "url": "http://localhost:1234",
    "temperature": 0.7,
    "max_tokens": 4096,
    "style": "detailed",  # detailed, concise, structured
    "auto_improve": True  # automatically improve prompts
}

def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            swallow.note('prompt_engineer.load_config:L35')
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config: Dict[str, Any]) -> bool:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Prompt engineer config save error: {e}")
        return False

def improve_prompt(
    user_prompt: str,
    call_llm_func,
    config: Optional[Dict[str, Any]] = None,
    task_context: Optional[str] = None
) -> Dict[str, Any]:
    """
    Улучшает промт пользователя через отдельную модель.
    
    Args:
        user_prompt: Исходный промт пользователя
        call_llm_func: Функция вызова LLM
        config: Конфигурация prompt engineer
        task_context: Дополнительный контекст задачи
    
    Returns:
        Dict с улучшенным промтом и метаданными
    """
    if config is None:
        config = load_config()
    
    if not config.get("enabled", False):
        return {
            "success": False,
            "enabled": False,
            "original_prompt": user_prompt
        }

    style = config.get("style", "detailed")
    
    # Build improvement prompt based on style
    if style == "detailed":
        system_prompt = """Ты Prompt Engineer Expert. Твоя задача — улучшить промт пользователя для AI-агента.

ПРАВИЛА:
1. Сохрани исходный смысл и намерение
2. Добавь конкретные детали и примеры если их нет
3. Укажи формат ответа который ожидается
4. Добавь контекст если задача сложная
5. Убери двусмысленности
6. Сделай промт понятным для автономного агента

ВАЖНО:
- Агент работает автономно через инструменты [TOOL:name]
- Не добавляй вопросов к пользователю
- Делай промт самодостаточным"""

    elif style == "structured":
        system_prompt = """Ты Prompt Engineer. Структурируй промт пользователя в чёткий формат.

ФОРМАТ ВЫВОДА:
[GOAL]: Главная цель задачи
[CONTEXT]: Необходимый контекст
[CONSTRAINTS]: Ограничения и требования
[FORMAT]: Ожидаемый формат ответа
[STEPS]: Рекомендуемые шаги выполнения

Убедись что промт понятен автономному AI-агенту."""

    else:  # concise
        system_prompt = """Ты Prompt Engineer. Сделай промт кратким но точным.
Убери лишнее, сохрани суть, добавь конкретику."""

    context_part = ""
    if task_context:
        context_part = f"\nКонтекст задачи: {task_context}"

    user_message = f"""Улучши этот промт:

{user_prompt}
{context_part}

Улучшенная версия:"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]

    try:
        improved = call_llm_func(
            messages,
            model=config.get("model", "gemma-3-4b-it"),
            lm_studio_url=config.get("url", "http://localhost:1234"),
            temperature=config.get("temperature", 0.7),
            max_tokens=config.get("max_tokens", 4096)
        )

        # Save to history
        history_entry = {
            "timestamp": time.time(),
            "original": user_prompt,
            "improved": improved,
            "style": style,
            "model_used": config.get("model")
        }
        _save_history_entry(history_entry)

        return {
            "success": True,
            "enabled": True,
            "original_prompt": user_prompt,
            "improved_prompt": improved,
            "style": style,
            "model_used": config.get("model"),
            "improvement_len": len(improved)
        }

    except Exception as e:
        swallow.note('prompt_engineer.improve_prompt:L157', e)
        return {
            "success": False,
            "enabled": True,
            "original_prompt": user_prompt,
            "error": str(e)
        }

def _save_history_entry(entry: Dict[str, Any]):
    history_file = os.path.join(PROMPT_ENG_DIR, "history.jsonl")
    try:
        with open(history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        swallow.note('prompt_engineer._save_history_entry:L171')
        pass

def get_history(limit: int = 20) -> list:
    history_file = os.path.join(PROMPT_ENG_DIR, "history.jsonl")
    entries = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        entries.append(json.loads(line))
        except Exception:
            swallow.note('prompt_engineer.get_history:L184')
            pass
    return entries[-limit:]
