"""
ASIO Agent Skill Registry v1.0
Регистрация пользовательских навыков/инструментов созданных агентом.
Skills сохраняются в JSON и загружаются при запуске.
"""

import os
import re
import json
import time
import ast
import logging
import threading
import uuid
from typing import Dict, List, Any, Optional, Callable
import swallow

logger = logging.getLogger("infinity")

SKILLS_DIR = os.path.expanduser("~/.infinity/asio_agent_v4/skills")
SKILLS_FILE = os.path.join(os.path.expanduser("~/.infinity/asio_agent_v4"), "registered_skills.json")

os.makedirs(SKILLS_DIR, exist_ok=True)

# Global registry
_registered_skills: Dict[str, Dict[str, Any]] = {}

def register_skill(name: str, description: str, tool_code: str, category: str = "custom", 
                    author: str = "agent", version: str = "1.0.0", auto_fix: bool = True, **_kw) -> Dict[str, Any]:
    """
    Регистрирует новый skill/инструмент.
    
    Args:
        name: Имя инструмента
        description: Описание
        tool_code: Python код
        category: Категория
        author: Автор
        version: Версия
        auto_fix: Автоматически исправлять проблемы
    
    Returns:
        {"success": bool, "output": str, "skill": dict}
    """
    # Normalize newlines
    normalized_code = tool_code.replace("\\n", "\n")
    
    # Validate before registration
    valid, error_msg = validate_skill_code(normalized_code)
    if not valid:
        if auto_fix:
            # Try to fix common issues
            normalized_code = _auto_fix_code(normalized_code)
            valid, error_msg = validate_skill_code(normalized_code)
        
        if not valid:
            return {"success": False, "output": f"❌ Валидация не пройдена: {error_msg}", "tool": "register_skill"}
    
    # Get existing version for history
    existing = _registered_skills.get(name)
    old_version = existing.get("version") if existing else "0.0.0"
    
    # Parse new version
    new_version = _bump_version(old_version) if not version or version == "auto" else version
    
    skill = {
        "name": name,
        "description": description,
        "code": normalized_code,
        "category": category,
        "author": author,
        "version": new_version,
        "registered_at": time.time(),
        "updated_at": time.time(),
        "version_history": existing.get("version_history", []) if existing else [],
        "stats": existing.get("stats", {
            "total_calls": 0,
            "successes": 0,
            "failures": 0,
            "avg_time_ms": 0,
            "min_time_ms": 0,
            "max_time_ms": 0,
            "last_errors": []
        }) if existing else {
            "total_calls": 0,
            "successes": 0,
            "failures": 0,
            "avg_time_ms": 0,
            "min_time_ms": 0,
            "max_time_ms": 0,
            "last_errors": []
        },
        "docstring": _extract_docstring(normalized_code),
        "parameters": _extract_parameters(normalized_code),
    }
    
    # Add to version history (keep last 10)
    version_history = skill["version_history"]
    version_history.append({
        "version": old_version,
        "code_hash": hash(normalized_code),
        "timestamp": time.time()
    })
    skill["version_history"] = version_history[-10:]
    
    _registered_skills[name] = skill
    _save_skills()
    logger.info(f"[SKILL] Registered: {name} v{new_version} ({category})")
    return {"success": True, "output": f"✅ Skill зарегистрирован: {name} v{new_version}\n{description}", "skill": skill}


def unregister_skill(name: str):
    """Удаляет зарегистрированный skill."""
    if name in _registered_skills:
        del _registered_skills[name]
        _save_skills()
        return {"success": True, "output": f"✅ Skill удалён: {name}"}
    return {"success": False, "output": f"❌ Skill не найден: {name}"}

def get_skill(name: str) -> Optional[Dict[str, Any]]:
    """Получить skill по имени."""
    return _registered_skills.get(name)

def list_skills(category: str = None) -> List[Dict[str, Any]]:
    """Список всех skills, опционально фильтруя по категории."""
    skills = list(_registered_skills.values())
    if category:
        skills = [s for s in skills if s.get("category") == category]
    return skills

def get_skills_description_for_prompt() -> str:
    """Возвращает описание skills для добавления в системный промпт."""
    if not _registered_skills:
        return ""
    
    lines = ["\n📦 CUSTOM TOOLS (зарегистрированные навыки):"]
    for name, skill in _registered_skills.items():
        lines.append(f"  [TOOL:{name}] — {skill['description']} (категория: {skill['category']})")
    return "\n".join(lines)

def increment_usage(name: str):
    """Увеличить счётчик использования."""
    if name in _registered_skills:
        _registered_skills[name]["usage_count"] = _registered_skills[name].get("usage_count", 0) + 1
        _registered_skills[name]["last_used"] = time.time()
        _save_skills()

def _save_skills():
    """Сохранить skills в JSON файл."""
    data = {
        "version": "1.0.0",
        "updated_at": time.time(),
        "skills": {name: skill for name, skill in _registered_skills.items()}
    }
    try:
        with open(SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[SKILL] Save error: {e}")

def load_skills():
    """Загрузить skills из JSON файла."""
    global _registered_skills
    _registered_skills = {}
    
    if not os.path.exists(SKILLS_FILE):
        return
    
    try:
        with open(SKILLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        _registered_skills = data.get("skills", {})
        logger.info(f"[SKILL] Loaded {len(_registered_skills)} skills")
    except Exception as e:
        logger.error(f"[SKILL] Load error: {e}")

def _parse_skill_args(input_str: str) -> tuple[str, str]:
    """
    Парсит входящие аргументы для execute_skill.
    
    Поддерживаемые форматы:
    - "my_test_tool" -> ("my_test_tool", "{}")
    - "my_test_tool | {}" -> ("my_test_tool", "{}")
    - "my_test_tool | {test: 123}" -> ("my_test_tool", "{test: 123}")
    - "name: my_test_tool" -> ("my_test_tool", "{}")
    - "name: my_test_tool | {test: 123}" -> ("my_test_tool", "{test: 123}")
    
    Returns:
        (tool_name, skill_args)
    """
    if not input_str:
        return "", "{}"
    
    input_str = input_str.strip()
    
    # First, handle "name:" prefix anywhere in the string
    # Pattern: "name: tool_name | {args}" or just "name: tool_name"
    name_prefix_match = re.match(r'^name:\s*(.+)$', input_str, re.IGNORECASE)
    if name_prefix_match:
        rest = name_prefix_match.group(1).strip()
        if "|" in rest:
            parts = rest.split("|", 1)
            name_part = _clean_tool_name(parts[0])
            args_part = parts[1].strip() if len(parts) > 1 else "{}"
            return name_part, args_part
        else:
            return _clean_tool_name(rest), "{}"
    
    # Формат: через "|" разделитель
    if "|" in input_str:
        parts = input_str.split("|", 1)
        name_part = parts[0].strip()
        args_part = parts[1].strip() if len(parts) > 1 else "{}"
        name_part = _clean_tool_name(name_part)
        return name_part, args_part
    
    # Просто имя
    return _clean_tool_name(input_str), "{}"


def _clean_tool_name(name: str) -> str:
    """Очищает имя инструмента от лишних символов."""
    name = name.strip()
    name = name.strip('"\'')
    name = name.strip('/')
    return name


def validate_skill_code(code: str) -> tuple[bool, str]:
    """
    Валидирует код скилла перед регистрацией.
    
    Returns:
        (is_valid, error_message)
    """
    if not code or not code.strip():
        return False, "Пустой код"
    
    # 1. Наличие функции execute
    if not re.search(r'def\s+execute\s*\(', code):
        return False, "Не найдена функция def execute(...)"
    
    # 2. Запрещённые паттерны ( Dangerous patterns) - ДО проверки syntax
    dangerous_patterns = [
        (r'os\.system\s*\(', 'os.system() запрещён (безопасность)'),
        (r'subprocess.*shell\s*=\s*True', 'subprocess с shell=True запрещён'),
        (r'__import__\s*\(', '__import__() запрещён'),
        (r'\beval\s*\(', 'eval() запрещён'),
        (r'\bexec\s*\(', 'exec() запрещён'),
        (r'open\s*\(\s*[\'"]\/etc', 'Доступ к /etc запрещён'),
        (r'open\s*\(\s*[\'"]\/root', 'Доступ к /root запрещён'),
        (r'rm\s+-rf', 'Удаление файлов запрещено'),
    ]
    
    for pattern, message in dangerous_patterns:
        if re.search(pattern, code):
            return False, f"Запрещённый паттерн: {message}"
    
    # 3. Проверка синтаксиса Python
    try:
        ast.parse(code)
    except SyntaxError as e:
        swallow.note('skill_registry.validate_skill_code:L263', e)
        return False, f"Синтаксическая ошибка: {e}"
    
    return True, "OK"


def _auto_fix_code(code: str) -> str:
    """Автоматически исправляет распространённые проблемы в коде."""
    # Fix: missing newline after function def
    code = re.sub(r'(def\s+execute\s*\([^)]*\))(\s+return)', r'\1:\n    return', code)
    
    # Fix: missing quotes around return string
    code = re.sub(r'return\s+([^\'\"\[{]+)$', r'return "\1"', code, flags=re.MULTILINE)
    
    return code


def _extract_docstring(code: str) -> str:
    """Извлекает docstring из кода функции."""
    match = re.search(r'def\s+execute\s*\([^)]*\):\s*"""([^"]*)"""', code, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"def\s+execute\s*\([^)]*\):\s*'''([^']*)'''", code, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def _extract_parameters(code: str) -> Dict[str, str]:
    """Извлекает параметры функции execute из кода."""
    match = re.search(r'def\s+execute\s*\(([^)]*)\)', code)
    if not match:
        return {}
    
    params = {}
    for param in match.group(1).split(','):
        param = param.strip()
        if '=' in param:
            name, default = param.split('=', 1)
            params[name.strip()] = default.strip()
        elif param:
            params[param] = "required"
    
    return params


def _bump_version(version: str) -> str:
    """Увеличивает версию."""
    try:
        parts = version.split('.')
        if len(parts) == 3:
            parts[2] = str(int(parts[2]) + 1)
            return '.'.join(parts)
    except Exception:
        swallow.note('skill_registry._bump_version:L317')
        pass
    return "1.0.1"


def update_skill_stats(name: str, success: bool, execution_time_ms: float, error: str = None):
    """Обновляет статистику выполнения."""
    if name not in _registered_skills:
        return
    
    stats = _registered_skills[name].get("stats", {})
    
    stats["total_calls"] = stats.get("total_calls", 0) + 1
    
    if success:
        stats["successes"] = stats.get("successes", 0) + 1
    else:
        stats["failures"] = stats.get("failures", 0) + 1
        # Add error to last_errors (keep last 5)
        errors = stats.get("last_errors", [])
        errors.append({"error": error, "timestamp": time.time()})
        stats["last_errors"] = errors[-5:]
    
    # Update timing
    avg = stats.get("avg_time_ms", 0)
    total = stats.get("total_calls", 1)
    stats["avg_time_ms"] = (avg * (total - 1) + execution_time_ms) / total
    
    if execution_time_ms < stats.get("min_time_ms", float('inf')) or stats.get("min_time_ms") == 0:
        stats["min_time_ms"] = execution_time_ms
    if execution_time_ms > stats.get("max_time_ms", 0):
        stats["max_time_ms"] = execution_time_ms
    
    _registered_skills[name]["stats"] = stats
    _registered_skills[name]["updated_at"] = time.time()


def execute_custom_skill(tool_name_or_args: str, skill_args: str = "{}", 
                        session=None, folder=None, lm_studio_url=None,
                        timeout: int = 30) -> Dict[str, Any]:
    """
    Выполняет кастомный skill с таймаутом и метриками.
    
    Args:
        tool_name_or_args: Имя скилла или строка с именем и аргументами
        skill_args: Аргументы для скилла (json строка)
        session: Сессия
        folder: Папка
        lm_studio_url: URL LM Studio
        timeout: Таймаут в секундах
    
    Returns:
        {"success": bool, "output": str, "tool": str, ...}
    """
    # Parse skill name and args
    tool_name, parsed_args = _parse_skill_args(tool_name_or_args)
    
    # Override skill_args if provided as separate param
    if skill_args and skill_args != "{}":
        parsed_args = skill_args
    
    if not tool_name:
        return {"success": False, "output": "❌ Не указано имя скилла", "tool": ""}
    
    if tool_name not in _registered_skills:
        return {"success": False, "output": f"❌ Custom skill не найден: {tool_name}", "tool": tool_name}
    
    skill = _registered_skills[tool_name]
    
    # Prepare result holder for threading
    result_holder = [None]
    error_holder = [None]
    exec_time = [0]
    
    def worker():
        start = time.time() * 1000
        try:
            result_holder[0] = _execute_skill_impl(tool_name, parsed_args, session, folder, lm_studio_url)
        except Exception as e:
            swallow.note('skill_registry.worker:L396', e)
            error_holder[0] = e
        finally:
            exec_time[0] = (time.time() * 1000) - start
    
    # Run with timeout
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout)
    
    if thread.is_alive():
        update_skill_stats(tool_name, False, timeout * 1000, f"Timeout after {timeout}s")
        return {"success": False, "output": f"❌ Таймаут выполнения ({timeout} сек)", "tool": tool_name, "timeout": True}
    
    if error_holder[0]:
        update_skill_stats(tool_name, False, exec_time[0], str(error_holder[0]))
        return {"success": False, "output": f"❌ Ошибка: {error_holder[0]}", "tool": tool_name}
    
    result = result_holder[0]
    if result:
        update_skill_stats(tool_name, result.get("success", False), exec_time[0])
    
    return result


def _execute_skill_impl(tool_name: str, skill_args: str, session, folder, lm_studio_url) -> Dict[str, Any]:
    """Внутренняя реализация выполнения скилла."""
    skill = _registered_skills[tool_name]
    code = skill["code"]
    code = code.replace("\\n", "\n")
    
    # Check for function
    func_match = re.search(r'def\s+(\w+)\s*\(', code)
    if func_match:
        func_name = func_match.group(1)
        
        # Build namespace with allowed imports and builtins
        namespace = {
            "args": skill_args,
            "session": session,
            "folder": folder,
            "lm_studio_url": lm_studio_url,
            # Core modules
            "os": __import__("os"),
            "re": __import__("re"),
            "json": __import__("json"),
            "time": __import__("time"),
            "logging": __import__("logging"),
            "subprocess": __import__("subprocess"),
            "sys": __import__("sys"),
            "urllib": __import__("urllib"),
            "urllib.request": __import__("urllib.request"),
            "logger": logger,
            # Allow common imports in skill code
            "print": print,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "sorted": sorted,
            "sum": sum,
            "min": min,
            "max": max,
            "abs": abs,
            "round": round,
            "any": any,
            "all": all,
            "open": open,
            "isinstance": isinstance,
            "hasattr": hasattr,
            "getattr": getattr,
        }
        
        exec(code, namespace)
        
        if func_name in namespace:
            func = namespace[func_name]
            
            # Try different call patterns
            call_patterns = [
                {"args": skill_args, "session": session, "folder": folder, "lm_studio_url": lm_studio_url},
                {"args": skill_args, "folder": folder},
                {"args": skill_args},
                {},
            ]
            
            result = None
            for kwargs in call_patterns:
                try:
                    result = func(**kwargs)
                    break
                except TypeError:
                    swallow.note('skill_registry._execute_skill_impl:L498')
                    continue
            
            if result is None:
                try:
                    result = func(skill_args)
                except Exception:
                    swallow.note('skill_registry._execute_skill_impl:L505')
                    result = {"success": False, "output": "Не удалось вызвать функцию"}
            
            if isinstance(result, dict):
                result["tool"] = tool_name
                return result
            return {"success": True, "output": str(result), "tool": tool_name}
        else:
            return {"success": False, "output": f"❌ Функция {func_name} не найдена", "tool": tool_name}
    else:
        # Execute script
        namespace = {
            "args": skill_args,
            "session": session,
            "folder": folder,
            "lm_studio_url": lm_studio_url,
            "os": __import__("os"),
            "re": __import__("re"),
            "json": __import__("json"),
            "time": __import__("time"),
            "logging": __import__("logging"),
            "subprocess": __import__("subprocess"),
            "sys": __import__("sys"),
            "urllib": __import__("urllib"),
            "urllib.request": __import__("urllib.request"),
            "logger": logger,
            # Allow common builtins
            "print": print, "len": len, "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict, "tuple": tuple, "set": set,
            "range": range, "enumerate": enumerate, "zip": zip, "map": map,
            "filter": filter, "sorted": sorted, "sum": sum, "min": min, "max": max,
            "abs": abs, "round": round, "any": any, "all": all, "open": open,
        }
        exec(code, namespace)
        return {"success": True, "output": "✅ Выполнено", "tool": tool_name}


def reload_skill(name: str = None) -> Dict[str, Any]:
    """
    Перезагружает скилл(ы) из файла.
    
    Args:
        name: Имя скилла или None для всех
    
    Returns:
        Результат операции
    """
    global _registered_skills
    
    if name:
        if name in _registered_skills:
            _registered_skills[name]["updated_at"] = time.time()
            logger.info(f"[SKILL] Reloaded: {name}")
            return {"success": True, "output": f"✅ Скилл перезагружен: {name}", "skill": _registered_skills[name]}
        return {"success": False, "output": f"❌ Скилл не найден: {name}"}
    
    # Reload all - re-read from file
    load_skills()
    return {"success": True, "output": f"✅ Перезагружено {len(_registered_skills)} скиллов"}


def get_skill_documentation() -> str:
    """
    Возвращает документацию всех скиллов для системного промпта.
    
    Формат:markdown описание всех tools
    """
    if not _registered_skills:
        return ""
    
    lines = ["\n## 📦 Custom Skills (зарегистрированные инструменты)\n"]
    
    for name, skill in sorted(_registered_skills.items()):
        version = skill.get("version", "1.0.0")
        description = skill.get("description", "Нет описания")
        category = skill.get("category", "custom")
        stats = skill.get("stats", {})
        docstring = skill.get("docstring", "")
        params = skill.get("parameters", {})
        
        lines.append(f"### `{name}`")
        lines.append(f"- **Описание**: {description}")
        lines.append(f"- **Категория**: {category}")
        lines.append(f"- **Версия**: {version}")
        
        if docstring:
            lines.append(f"- **Документация**: {docstring}")
        
        if params:
            params_str = ", ".join([f"{k}" for k in params.keys()])
            lines.append(f"- **Параметры**: {params_str}")
        
        if stats:
            calls = stats.get("total_calls", 0)
            if calls > 0:
                success = stats.get("successes", 0)
                avg_time = stats.get("avg_time_ms", 0)
                lines.append(f"- **Статистика**: {calls} вызовов, {success} успешно, среднее {avg_time:.1f}мс")
        
        lines.append("")
    
    return "\n".join(lines)


def get_skill_info(name: str) -> Dict[str, Any]:
    """Возвращает полную информацию о скилле."""
    if name not in _registered_skills:
        return {"success": False, "output": f"❌ Скилл не найден: {name}"}
    
    skill = _registered_skills[name]
    return {
        "success": True,
        "name": skill.get("name"),
        "description": skill.get("description"),
        "version": skill.get("version"),
        "category": skill.get("category"),
        "author": skill.get("author"),
        "docstring": skill.get("docstring"),
        "parameters": skill.get("parameters"),
        "stats": skill.get("stats"),
        "registered_at": skill.get("registered_at"),
        "updated_at": skill.get("updated_at"),
        "version_history": skill.get("version_history", [])[-3:],
    }


def execute_skill_async(tool_name: str, skill_args: str = "{}", 
                        session=None, folder=None, lm_studio_url=None,
                        timeout: int = 30) -> Dict[str, Any]:
    """
    Асинхронное выполнение скилла.
    
    Returns task_id для отслеживания результата.
    """
    task_id = str(uuid.uuid4())[:8]
    result_holder = [None]
    error_holder = [None]
    
    def worker():
        try:
            result_holder[0] = execute_custom_skill(
                tool_name, skill_args, session, folder, lm_studio_url, timeout
            )
        except Exception as e:
            swallow.note('skill_registry.worker:L649', e)
            error_holder[0] = e
    
    thread = threading.Thread(target=worker)
    thread.start()
    
    return {
        "success": True,
        "output": f"🔄 Задача запущена",
        "tool": tool_name,
        "task_id": task_id,
        "async": True
    }


# Load skills on import
load_skills()
