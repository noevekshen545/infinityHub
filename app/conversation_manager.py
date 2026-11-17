"""
ASIO Conversation & Memory Manager v1.0
Объединяет 4 функции:
1. Conversation Archive — сохранение переписки и аргументов агента
2. Memory Summarization — авто-суммаризация памяти
3. Memory Index — система оглавлений для быстрой навигации
4. Context Compression — обход ограничения контекстного окна
"""

import os
import re
import json
import time
import hashlib
import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import swallow

logger = logging.getLogger("infinity")

# Directories
MEMORY_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
ARCHIVE_DIR = os.path.join(MEMORY_BASE, "conversation_archive")
SUMMARY_DIR = os.path.join(MEMORY_BASE, "memory_summaries")
INDEX_FILE = os.path.join(MEMORY_BASE, "memory_index.json")
COMPRESSED_FILE = os.path.join(MEMORY_BASE, "context_compressed.json")

os.makedirs(ARCHIVE_DIR, exist_ok=True)
os.makedirs(SUMMARY_DIR, exist_ok=True)

# === 1. CONVERSATION ARCHIVE ===

def archive_conversation(session_id: str, messages: List[Dict], agent_reasoning: List[Dict] = None):
    """
    Сохраняет полную переписку сессии включая аргументы/решения агента.
    """
    try:
        # Create archive file
        archive_file = os.path.join(ARCHIVE_DIR, f"{session_id}.json")
        
        archive_data = {
            "session_id": session_id,
            "archived_at": time.time(),
            "message_count": len(messages),
            "messages": messages,
            "agent_reasoning": agent_reasoning or [],
            "summary": _generate_conversation_summary(messages),
            "topics": _extract_topics(messages),
            "decisions": _extract_decisions(messages, agent_reasoning)
        }
        
        with open(archive_file, "w", encoding="utf-8") as f:
            json.dump(archive_data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"[ARCHIVE] Saved conversation: {session_id} ({len(messages)} messages)")
        return {"success": True, "path": archive_file, "messages": len(messages)}
    except Exception as e:
        logger.error(f"[ARCHIVE] Error: {e}")
        return {"success": False, "error": str(e)}

def search_archive(query: str, limit: int = 5) -> List[Dict]:
    """Поиск по архиву переписки."""
    results = []
    query_lower = query.lower()
    
    for fn in os.listdir(ARCHIVE_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(ARCHIVE_DIR, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Search in messages, summary, topics, decisions
            searchable = json.dumps(data, ensure_ascii=False).lower()
            if query_lower in searchable:
                results.append({
                    "session_id": data["session_id"],
                    "summary": data.get("summary", "")[:300],
                    "topics": data.get("topics", []),
                    "decisions": data.get("decisions", [])[:3],
                    "messages": data.get("message_count", 0),
                    "date": datetime.fromtimestamp(data.get("archived_at", 0)).strftime("%Y-%m-%d %H:%M"),
                    "relevance": _calculate_relevance(data, query_lower)
                })
        except Exception:
            swallow.note('conversation_manager.search_archive:L86')
            continue
    
    # Sort by relevance
    results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
    return results[:limit]

def _generate_conversation_summary(messages: List[Dict]) -> str:
    """Генерирует краткое содержание переписки."""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    
    if not user_msgs:
        return "Пустая сессия"
    
    # Extract key points
    first_user = user_msgs[0].get("content", "")[:200]
    last_assistant = assistant_msgs[-1].get("content", "")[:200] if assistant_msgs else ""
    
    summary = f"Запрос: {first_user}"
    if last_assistant:
        summary += f"\nОтвет: {last_assistant}"
    summary += f"\nВсего сообщений: {len(messages)}"
    
    return summary[:500]

def _extract_topics(messages: List[Dict]) -> List[str]:
    """Извлекает темы из переписки."""
    text = " ".join(m.get("content", "") for m in messages).lower()
    
    topic_keywords = {
        "файлы": ["файл", "file", "читать", "записать", "создать"],
        "код": ["код", "code", "python", "функци", "класс"],
        "поиск": ["поиск", "search", "найти", "искать"],
        "анализ": ["анализ", "analysis", "данные", "data"],
        "настройка": ["настройк", "config", "конфиг", "параметр"],
        "ошибки": ["ошибк", "error", "баг", "fix", "исправ"],
    }
    
    topics = []
    for topic, keywords in topic_keywords.items():
        if any(kw in text for kw in keywords):
            topics.append(topic)
    
    return topics[:5]

def _extract_decisions(messages: List[Dict], reasoning: List[Dict] = None) -> List[str]:
    """Извлекает решения/аргументы агента."""
    decisions = []
    
    for m in messages:
        content = m.get("content", "")
        # Look for decision patterns
        decision_patterns = [
            r"реш[аи].*?(.{20,100})",
            r"выбр[аи].*?(.{20,100})",
            r"потому что.*?(.{20,100})",
            r"поэтому.*?(.{20,100})",
        ]
        for pattern in decision_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                decisions.append(match.group(0).strip()[:150])
    
    if reasoning:
        for r in reasoning:
            if r.get("text"):
                decisions.append(r["text"][:150])
    
    return list(set(decisions))[:10]

def _calculate_relevance(archive_data: Dict, query: str) -> float:
    """Вычисляет релевантность результата поиска."""
    score = 0.0
    searchable = json.dumps(archive_data, ensure_ascii=False).lower()
    
    # Exact match in summary
    if query in archive_data.get("summary", "").lower():
        score += 10
    # Match in topics
    if any(query in t.lower() for t in archive_data.get("topics", [])):
        score += 5
    # Match in decisions
    if any(query in d.lower() for d in archive_data.get("decisions", [])):
        score += 3
    # General text match
    score += searchable.count(query) * 0.5
    
    return score

def archive_session_if_needed(session_id: str, messages: List[Dict], 
                                max_messages: int = 50):
    """Автоматически архивирует сессию если сообщений больше лимита."""
    if len(messages) > max_messages:
        return archive_conversation(session_id, messages)
    return None

# === 2. MEMORY SUMMARIZATION ===

def summarize_core_memory(min_items: int = 50, compression_ratio: float = 0.5):
    """
    Суммаризация core memory — сжимает длинные записи.
    Если элементов > min_items, суммаризует до compression_ratio.
    """
    from agent_module import CORE_MEMORY_FILE, load_core_memory, save_core_memory
    
    if not os.path.exists(CORE_MEMORY_FILE):
        return {"success": False, "output": "Core memory не существует"}
    
    memory = load_core_memory()
    total_items = sum(len(v) for v in memory.values() if isinstance(v, list))
    
    if total_items < min_items:
        return {"success": True, "output": f"Память компактная: {total_items} элементов (порог: {min_items})", "compressed": False}
    
    # Summarize each category
    compressed = {}
    saved_count = 0
    
    for key, items in memory.items():
        if not isinstance(items, list):
            compressed[key] = items
            continue
        
        if len(items) > min_items * 0.3:  # Compress if category is large
            # Keep newest items + create summary of old ones
            keep_count = max(10, int(len(items) * compression_ratio))
            old_items = items[:-keep_count]
            new_items = items[-keep_count:]
            
            # Create summary of old items
            summary_text = _summarize_items(old_items)
            summary_item = {
                "type": "summary",
                "content": summary_text,
                "original_count": len(old_items),
                "summarized_at": time.time()
            }
            
            compressed[key] = [summary_item] + new_items
            saved_count += len(old_items) - 1
        else:
            compressed[key] = items
    
    # Save compressed memory
    save_core_memory(compressed)
    
    # Also save summary to file
    summary_file = os.path.join(SUMMARY_DIR, f"summary_{int(time.time())}.json")
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({"original_count": total_items, "compressed": True}, f)
    
    logger.info(f"[MEMORY] Compressed: {total_items} -> ~{total_items - saved_count} items (saved {saved_count})")
    return {
        "success": True, 
        "output": f"✅ Память суммаризирована: {total_items} → ~{total_items - saved_count} элементов\nСохранено: {saved_count} записей",
        "compressed": True,
        "saved_items": saved_count
    }

def _summarize_items(items: List) -> str:
    """Суммаризует список элементов в краткий текст."""
    if not items:
        return ""
    
    # Group by type/category
    summaries = []
    
    for item in items[:20]:  # Sample first 20
        if isinstance(item, dict):
            content = item.get("content", str(item))[:100]
            summaries.append(content)
        elif isinstance(item, str):
            summaries.append(item[:100])
    
    # Create summary text
    if len(summaries) > 5:
        summary = f"[Суммаризировано {len(items)} записей. Ключевые: " + "; ".join(summaries[:5]) + "]"
    else:
        summary = "; ".join(summaries)
    
    return summary[:500]

# === 3. MEMORY INDEX (Оглавления) ===

def build_memory_index():
    """
    Строит индекс/оглавление памяти для быстрой навигации.
    Создаёт карту: какие данные есть, где искать, ключевые слова.
    """
    from agent_module import CORE_MEMORY_FILE, load_core_memory
    
    index = {
        "built_at": time.time(),
        "categories": {},
        "keywords": {},
        "sessions": {},
        "skills": {},
        "rules": {},
        "total_size": 0
    }
    
    # Index core memory
    if os.path.exists(CORE_MEMORY_FILE):
        memory = load_core_memory()
        for key, items in memory.items():
            if isinstance(items, list):
                index["categories"][key] = {
                    "count": len(items),
                    "keywords": _extract_keywords(items),
                    "last_updated": max((i.get("time", 0) for i in items if isinstance(i, dict)), default=0)
                }
                index["total_size"] += len(items)
    
    # Index sessions
    session_dir = os.path.join(MEMORY_BASE, "sessions")
    if os.path.exists(session_dir):
        for fn in os.listdir(session_dir):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(session_dir, fn), "r", encoding="utf-8") as f:
                        session = json.load(f)
                    session_index = {
                        "messages": len(session.get("messages", [])),
                        "created": session.get("created_at", 0),
                        "topics": _extract_topics(session.get("messages", []))
                    }
                    index["sessions"][fn.replace(".json", "")] = session_index
                except Exception:
                    swallow.note('conversation_manager.build_memory_index:L315')
                    pass
    
    # Index skills
    skills_file = os.path.join(MEMORY_BASE, "registered_skills.json")
    if os.path.exists(skills_file):
        try:
            with open(skills_file, "r", encoding="utf-8") as f:
                skills_data = json.load(f)
            index["skills"] = {name: skill.get("description", "")[:100] 
                              for name, skill in skills_data.get("skills", {}).items()}
        except Exception:
            swallow.note('conversation_manager.build_memory_index:L327')
            pass
    
    # Index rules
    rules_file = os.path.join(MEMORY_BASE, "agent_rules.json")
    if os.path.exists(rules_file):
        try:
            with open(rules_file, "r", encoding="utf-8") as f:
                rules_data = json.load(f)
            index["rules"] = {r["id"]: r["name"] for r in rules_data.get("rules", [])}
        except Exception:
            swallow.note('conversation_manager.build_memory_index:L338')
            pass
    
    # Save index
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    
    logger.info(f"[INDEX] Built memory index: {index['total_size']} items, {len(index['sessions'])} sessions")
    return index

def get_memory_index() -> Dict:
    """Получить текущий индекс."""
    if not os.path.exists(INDEX_FILE):
        return build_memory_index()
    
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def search_index(query: str) -> Dict:
    """Быстрый поиск по индексу (без загрузки всей памяти)."""
    index = get_memory_index()
    results = {"categories": [], "sessions": [], "skills": [], "rules": []}
    query_lower = query.lower()
    
    # Search in categories
    for cat, info in index.get("categories", {}).items():
        if query_lower in cat.lower() or any(query_lower in kw.lower() for kw in info.get("keywords", [])):
            results["categories"].append({"name": cat, **info})
    
    # Search in sessions
    for sid, info in index.get("sessions", {}).items():
        if query_lower in sid.lower() or any(query_lower in t.lower() for t in info.get("topics", [])):
            results["sessions"].append({"id": sid, **info})
    
    # Search in skills
    for name, desc in index.get("skills", {}).items():
        if query_lower in name.lower() or query_lower in desc.lower():
            results["skills"].append({"name": name, "description": desc})
    
    return results

def _extract_keywords(items: List) -> List[str]:
    """Извлекает ключевые слова из элементов."""
    keywords = set()
    for item in items[:20]:
        text = item.get("content", str(item)) if isinstance(item, dict) else str(item)
        # Extract words longer than 4 chars
        for word in re.findall(r'\b\w{4,}\b', text.lower()):
            if word not in {'это', 'что', 'как', 'для', 'более', 'менее', 'также'}:
                keywords.add(word)
    return list(keywords)[:30]

# === 4. CONTEXT COMPRESSION ===

def compress_context(messages: List[Dict], max_tokens: int = 8000, 
                     preserve_recent: int = 10) -> List[Dict]:
    """
    Сжимает контекст сообщений чтобы уместиться в лимит токенов.
    - Сохраняет последние preserve_recent сообщений полностью
    - Суммаризует старые сообщения
    - Удаляет дубликаты и пустые сообщения
    """
    if not messages:
        return messages
    
    # Estimate token count (rough: 1 token ≈ 4 chars for English, 1-2 for Russian)
    def estimate_tokens(text: str) -> int:
        return len(text) // 3  # Conservative estimate for mixed text
    
    total_chars = sum(len(m.get("content", "")) for m in messages)
    estimated_tokens = total_chars // 3
    
    if estimated_tokens <= max_tokens:
        return messages  # No compression needed
    
    # Keep system message and recent messages
    system_msg = next((m for m in messages if m.get("role") == "system"), None)
    recent_msgs = messages[-preserve_recent:]
    old_msgs = [m for m in messages[:-preserve_recent] if m.get("role") != "system"]
    
    # Compress old messages
    compressed_old = _compress_old_messages(old_msgs, max_tokens)
    
    # Build compressed context
    result = []
    if system_msg:
        result.append(system_msg)
    
    # Add summary of old messages
    if compressed_old:
        summary_content = f"[Суммаризировано {len(old_msgs)} предыдущих сообщений]\n" + "\n".join(compressed_old)
        result.append({
            "role": "user",
            "content": summary_content[:max_tokens * 2],  # Limit summary
            "compressed": True
        })
    
    result.extend(recent_msgs)
    
    logger.info(f"[COMPRESS] Context: {len(messages)} -> {len(result)} msgs, ~{estimated_tokens} -> ~{max_tokens} tokens")
    return result

def _compress_old_messages(messages: List[Dict], target_tokens: int) -> List[str]:
    """Суммаризует старые сообщения."""
    if not messages:
        return []
    
    # Group by role
    user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
    assistant_msgs = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
    tool_msgs = [m for m in messages if m.get("role") == "user" and "Результат" in m.get("content", "")]
    
    compressed = []
    
    # Summarize user requests
    if user_msgs:
        summary = f"Пользователь задал {len(user_msgs)} вопросов/запросов"
        # Keep first and last user message
        if user_msgs:
            summary += f"\nПервый: {user_msgs[0][:200]}"
            if len(user_msgs) > 1:
                summary += f"\nПоследний: {user_msgs[-1][:200]}"
        compressed.append(summary)
    
    # Summarize assistant responses
    if assistant_msgs:
        summary = f"Агент дал {len(assistant_msgs)} ответов"
        # Extract tool usage summary
        tool_usage = {}
        for msg in assistant_msgs:
            tool_match = re.search(r'\[TOOL:(\w+)\]', msg)
            if tool_match:
                tool_name = tool_match.group(1)
                tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
        
        if tool_usage:
            tools_summary = ", ".join(f"{k}: {v} раз" for k, v in tool_usage.items())
            summary += f"\nИспользованы инструменты: {tools_summary}"
        
        compressed.append(summary)
    
    return compressed

def auto_compress_if_needed(session_id: str, messages: List[Dict]) -> Tuple[List[Dict], bool]:
    """Автоматически сжимает контекст если превышает лимит."""
    max_tokens = 8000
    compressed = compress_context(messages, max_tokens)
    
    if len(compressed) < len(messages):
        # Save compressed version
        compressed_file = os.path.join(MEMORY_BASE, "context_compressed.json")
        with open(compressed_file, "w", encoding="utf-8") as f:
            json.dump({"session": session_id, "original_count": len(messages), "compressed_count": len(compressed), "compressed_at": time.time()}, f)
        return compressed, True
    
    return messages, False

# Auto-archive on import check
def check_and_maintain():
    """Проверяет и обслуживает память/архив."""
    try:
        # Check if index is stale (>1 hour)
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE, "r") as f:
                idx = json.load(f)
            if time.time() - idx.get("built_at", 0) > 3600:
                build_memory_index()
        else:
            build_memory_index()
    except Exception as e:
        logger.debug(f"[MANAGER] Maintenance check: {e}")

check_and_maintain()
