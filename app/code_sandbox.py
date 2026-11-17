"""
Code Sandbox Module
Безопасное выполнение кода в Docker контейнере
"""

import os
import json
import time
import uuid
import logging
import subprocess
import shutil
from typing import Dict, Optional, Any, List
from pathlib import Path
import swallow

logger = logging.getLogger("infinity")

# Конфигурация
SANDBOX_DIR = os.path.expanduser("~/.infinity/sandbox")
os.makedirs(SANDBOX_DIR, exist_ok=True)

# Docker настройки
DOCKER_ENABLED = False
DOCKER_IMAGE = "python:3.12-slim"
DOCKER_TIMEOUT = 60
DOCKER_MEMORY_LIMIT = "512m"
DOCKER_CPU_LIMIT = "1.0"


def check_docker() -> bool:
    """Проверяет доступен ли Docker."""
    global DOCKER_ENABLED
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        DOCKER_ENABLED = result.returncode == 0
        if DOCKER_ENABLED:
            logger.info(f"Docker available: {result.stdout.strip()}")
        else:
            logger.warning("Docker not available")
        return DOCKER_ENABLED
    except Exception as e:
        logger.warning(f"Docker check failed: {e}")
        DOCKER_ENABLED = False
        return False


def execute_in_sandbox(
    code: str,
    language: str = "python",
    timeout: int = DOCKER_TIMEOUT,
    env_vars: Optional[Dict[str, str]] = None,
    allowed_packages: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Выполняет код в Docker контейнере.
    
    Args:
        code: Код для выполнения
        language: Язык программирования
        timeout: Таймаут в секундах
        env_vars: Переменные окружения
        allowed_packages: Разрешённые pip пакеты
        
    Returns:
        dict с результатом выполнения
    """
    if not DOCKER_ENABLED:
        return {
            "success": False,
            "error": "Docker недоступен. Используйте локальное выполнение.",
            "fallback_to_local": True
        }
    
    # Создаём временную директорию
    session_id = f"sandbox_{uuid.uuid4().hex[:8]}"
    work_dir = os.path.join(SANDBOX_DIR, session_id)
    os.makedirs(work_dir, exist_ok=True)
    
    try:
        # Определяем расширение и файл
        ext_map = {
            "python": ".py",
            "javascript": ".js",
            "typescript": ".ts",
            "ruby": ".rb",
            "php": ".php"
        }
        ext = ext_map.get(language, ".txt")
        filename = f"main{ext}"
        filepath = os.path.join(work_dir, filename)
        
        # Записываем код
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)
        
        # Создаём Dockerfile если нужны пакеты
        dockerfile_content = f"FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app\n"
        if allowed_packages:
            dockerfile_content += f"RUN pip install --break-system-packages {' '.join(allowed_packages)}\n"
        
        dockerfile_path = os.path.join(work_dir, "Dockerfile")
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)
        
        # Формируем команду
        lang_commands = {
            "python": ["python3", filename],
            "javascript": ["node", filename],
            "typescript": ["npx", "ts-node", filename],
            "ruby": ["ruby", filename],
            "php": ["php", filename]
        }
        
        cmd = lang_commands.get(language, ["python3", filename])
        
        docker_cmd = [
            "docker", "run",
            "--rm",
            "--network", "none",  # Отключаем сеть для безопасности
            "--memory", DOCKER_MEMORY_LIMIT,
            "--cpus", DOCKER_CPU_LIMIT,
            "--workdir", "/app",
            "-v", f"{work_dir}:/app",
        ]
        
        # Добавляем переменные окружения
        if env_vars:
            for key, value in env_vars.items():
                docker_cmd.extend(["-e", f"{key}={value}"])
        
        docker_cmd.extend([
            DOCKER_IMAGE,
            *cmd
        ])
        
        # Выполняем
        logger.info(f"[SANDBOX] Executing {language} code in Docker container")
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        output = result.stdout
        if result.stderr:
            output += "\n[STDERR]\n" + result.stderr
        
        if result.returncode != 0:
            output += f"\n[EXIT CODE: {result.returncode}]"
        
        return {
            "success": result.returncode == 0,
            "output": output[:10000],
            "exit_code": result.returncode,
            "session_id": session_id,
            "method": "docker"
        }
        
    except subprocess.TimeoutExpired:
        swallow.note('code_sandbox.execute_in_sandbox:L166')
        return {
            "success": False,
            "error": f"Таймаут ({timeout}с)",
            "session_id": session_id,
            "method": "docker"
        }
    except Exception as e:
        logger.error(f"Sandbox execution error: {e}")
        return {
            "success": False,
            "error": str(e),
            "session_id": session_id,
            "method": "docker"
        }


def cleanup_sandbox(session_id: str) -> bool:
    """Очищает sandbox директорию."""
    work_dir = os.path.join(SANDBOX_DIR, session_id)
    if os.path.exists(work_dir):
        try:
            shutil.rmtree(work_dir)
            return True
        except Exception:
            swallow.note('code_sandbox.cleanup_sandbox:L191')
            return False
    return False


def cleanup_all_old_sessions(max_age_hours: int = 24) -> int:
    """Очищает все старые сессии."""
    cleaned = 0
    cutoff_time = time.time() - (max_age_hours * 3600)
    
    for session_dir in os.listdir(SANDBOX_DIR):
        dir_path = os.path.join(SANDBOX_DIR, session_dir)
        if os.path.isdir(dir_path):
            mtime = os.path.getmtime(dir_path)
            if mtime < cutoff_time:
                try:
                    shutil.rmtree(dir_path)
                    cleaned += 1
                except Exception:
                    swallow.note('code_sandbox.cleanup_all_old_sessions:L210')
                    pass
    
    return cleaned


def get_sandbox_stats() -> Dict[str, Any]:
    """Получает статистику sandbox."""
    sessions = []
    total_size = 0
    
    for session_dir in os.listdir(SANDBOX_DIR):
        dir_path = os.path.join(SANDBOX_DIR, session_dir)
        if os.path.isdir(dir_path):
            try:
                size = sum(
                    os.path.getsize(os.path.join(dir_path, f))
                    for f in os.listdir(dir_path)
                    if os.path.isfile(os.path.join(dir_path, f))
                )
                total_size += size
                sessions.append({
                    "id": session_dir,
                    "size": size,
                    "modified": os.path.getmtime(dir_path)
                })
            except Exception:
                swallow.note('code_sandbox.get_sandbox_stats:L237')
                pass
    
    return {
        "total_sessions": len(sessions),
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "docker_enabled": DOCKER_ENABLED,
        "sessions": sorted(sessions, key=lambda x: x["modified"], reverse=True)[:10]
    }


# Автопроверка Docker при импорте
check_docker()
