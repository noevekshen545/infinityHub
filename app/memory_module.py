#!/usr/bin/env python3
"""
Модуль памяти AI — долгосрочное хранение фактов, предпочтений и контекста.
"""

import os, json, time, hashlib, re
import swallow

# Загружаем конфигурацию
try:
    from config_module import get_config
    _CFG = get_config()
    MEMORY_DIR = _CFG.get("memory_dir", os.path.expanduser("~/notes/_memory"))
except Exception:
    swallow.note('memory_module.<module>:L14')
    MEMORY_DIR = os.path.expanduser("~/pars/notes/_memory")
os.makedirs(MEMORY_DIR, exist_ok=True)


def _dir():
    """Каталог памяти профиля запроса.

    MEMORY_DIR считался один раз при импорте и был общим для всех профилей:
    список и поиск по памяти показывали чужие факты, а «Очистить память»
    сносила их всем. Вне запроса (фон, CLI, индексация) остаётся прежний
    модульный каталог — иначе фон писал бы в каталог активного профиля.
    """
    try:
        import profile_paths
        pid = profile_paths.current_profile_id()
        if pid:
            return profile_paths.dir_for(pid, "MEMORY_DIR")
    except Exception:
        swallow.note('memory_module._dir:L31')
    return MEMORY_DIR

# Категории памяти
MEMORY_CATEGORIES = {
    "facts": "Факты и знания",
    "preferences": "Предпочтения пользователя",
    "context": "Контекст разговоров",
    "corrections": "Исправления и уточнения",
    "goals": "Цели и задачи",
    "general": "Общее",
}

def _memory_file(category, key):
    """Путь к файлу памяти."""
    safe_key = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9_\-]', '_', key)[:50]
    h = hashlib.md5(f"{category}:{key}".encode()).hexdigest()[:8]
    return os.path.join(_dir(), f"{category}_{safe_key}_{h}.json")

def save_memory(category, key, value, metadata=None):
    """Сохранить факт в память."""
    if category not in MEMORY_CATEGORIES:
        category = "general"
    fp = _memory_file(category, key)
    data = {
        "category": category,
        "key": key,
        "value": value,
        "metadata": metadata or {},
        "created": time.time(),
        "updated": time.time(),
        "access_count": 0,
    }
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                existing = json.load(f)
            data["created"] = existing.get("created", data["created"])
            data["access_count"] = existing.get("access_count", 0)
        except Exception:
            swallow.note('memory_module.save_memory:L55')
            pass
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        swallow.note('memory_module.save_memory:L62')
        return False

def get_memory(category, key):
    """Получить факт из памяти."""
    fp = _memory_file(category, key)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        data["access_count"] = data.get("access_count", 0) + 1
        data["updated"] = time.time()
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    except Exception:
        swallow.note('memory_module.get_memory:L79')
        return None

def search_memory(query, category=None, limit=10):
    """Поиск по памяти."""
    results = []
    ql = query.lower()
    mem_dir = _dir()
    try:
        for fn in os.listdir(mem_dir):
            if not fn.endswith(".json"):
                continue
            if category and not fn.startswith(f"{category}_"):
                continue
            try:
                with open(os.path.join(mem_dir, fn), encoding="utf-8") as f:
                    data = json.load(f)
                text = f"{data.get('key', '')} {data.get('value', '')} {json.dumps(data.get('metadata', {}))}"
                if ql in text.lower():
                    results.append(data)
            except Exception:
                swallow.note('memory_module.search_memory:L99')
                continue
    except Exception:
        swallow.note('memory_module.search_memory:L102')
        pass
    results.sort(key=lambda x: (x.get("access_count", 0), x.get("updated", 0)), reverse=True)
    return results[:limit]

def list_memories(category=None, page=1, per_page=20):
    """Список всех записей памяти."""
    memories = []
    mem_dir = _dir()
    try:
        for fn in os.listdir(mem_dir):
            if not fn.endswith(".json"):
                continue
            if category and not fn.startswith(f"{category}_"):
                continue
            try:
                with open(os.path.join(mem_dir, fn), encoding="utf-8") as f:
                    data = json.load(f)
                memories.append({
                    "key": data.get("key", ""),
                    "category": data.get("category", ""),
                    "value_preview": str(data.get("value", ""))[:100],
                    "created": data.get("created", 0),
                    "updated": data.get("updated", 0),
                    "access_count": data.get("access_count", 0),
                })
            except Exception:
                swallow.note('memory_module.list_memories:L128')
                continue
    except Exception:
        swallow.note('memory_module.list_memories:L131')
        pass
    memories.sort(key=lambda x: x.get("updated", 0), reverse=True)
    total = len(memories)
    start = (page - 1) * per_page
    return {
        "memories": memories[start:start + per_page],
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }

def delete_memory(category, key):
    """Удалить запись из памяти."""
    fp = _memory_file(category, key)
    if os.path.exists(fp):
        os.remove(fp)
        return True
    return False

def clear_memory(category=None):
    """Очистить память (всю или категорию)."""
    try:
        mem_dir = _dir()
        for fn in os.listdir(mem_dir):
            if not fn.endswith(".json"):
                continue
            if category and not fn.startswith(f"{category}_"):
                continue
            os.remove(os.path.join(mem_dir, fn))
        return True
    except Exception:
        swallow.note('memory_module.clear_memory:L162')
        return False

def extract_facts_from_conversation(question, answer):
    """Извлечь факты из разговора для сохранения в память."""
    facts = []
    patterns = [
        (r'я\s+(?:люблю|предпочитаю|хочу|желаю|нравится|обожаю)\s+(.+?)[\.\,\!\?]', 'preferences'),
        (r'(?:зовут|имя)\s+(?:меня|его|её)\s+([A-ZА-ЯЁ][a-zA-Zа-яА-ЯёЁ]+)', 'facts'),
        (r'(?:мне\s+\d+)\s+(?:лет|год|года)', 'facts'),
        (r'(?:работаю|учусь)\s+(?:как|в|на)\s+(.+?)[\.\,\!\?]', 'facts'),
        (r'(?:моя\s+(?:цель|задача|мечта))\s+(?:—|-|:)?\s+(.+?)[\.\,\!\?]', 'goals'),
    ]
    for pattern, category in patterns:
        matches = re.findall(pattern, answer, re.IGNORECASE)
        for m in matches:
            facts.append({"category": category, "key": m.strip()[:50], "value": m.strip()})
    return facts
