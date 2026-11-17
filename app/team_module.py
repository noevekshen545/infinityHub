"""
ASIO Agent Team Module
Управление командами агентов (как AutoGen)
"""

import os
import json
import time
import uuid
import logging
from typing import List, Dict, Optional, Any, Callable
from pathlib import Path

logger = logging.getLogger("infinity")

# === DIRECTORIES (consistent with agent_module v4) ===
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
TEAMS_DIR = os.path.join(AGENT_BASE, "teams")
os.makedirs(TEAMS_DIR, exist_ok=True)

# Доступные типы агентов
AGENT_TYPES = {
    "researcher": {
        "name": "Исследователь",
        "description": "Ищет информацию в интернете и документах",
        "prompt": "Ты исследователь. Твоя задача - найти информацию в интернете, проанализировать источники и предоставить подробный отчёт. Используй web_search, читай статьи, делай summary.",
        "allowed_tools": ["web_search", "read_file", "write_file", "shell"],
        "icon": "🔍"
    },
    "coder": {
        "name": "Программист",
        "description": "Пишет код на Python, JS, и других языках",
        "prompt": "Ты программист. Пиши чистый, рабочий код с обработкой ошибок. Всегда тестируй код после написания. Используй best practices.",
        "allowed_tools": ["write_file", "read_file", "run_code", "shell", "install"],
        "icon": "💻"
    },
    "tester": {
        "name": "Тестировщик",
        "description": "Создаёт и запускает unit/integration тесты",
        "prompt": "Ты тестировщик. Создавай unit тесты, integration тесты. Запускай их, анализируй результаты. Если тест падает - найди причину.",
        "allowed_tools": ["write_file", "read_file", "run_code", "shell"],
        "icon": "🧪"
    },
    "debugger": {
        "name": "Отладчик",
        "description": "Анализирует ошибки и находит причины",
        "prompt": "Ты отладчик. Анализируй stack traces, логи, ошибки. Находи корневую причину (root cause). Предлагай исправления.",
        "allowed_tools": ["read_file", "shell", "list_dir", "debug"],
        "icon": "🐛"
    },
    "architect": {
        "name": "Архитектор",
        "description": "Проектирует структуру проекта и модули",
        "prompt": "Ты архитектор. Проектируй модульную, масштабируемую архитектуру. Следуй SOLID принципам. Предлагай паттерны проектирования.",
        "allowed_tools": ["read_file", "list_dir", "scan_project", "write_file"],
        "icon": "🏗️"
    },
    "reviewer": {
        "name": "Ревьюер",
        "description": "Проверяет код на best practices и баги",
        "prompt": "Ты ревьюер. Проверяй код на: баги, security vulnerabilities, performance issues, code style, best practices. Будь критичен но конструктивен.",
        "allowed_tools": ["read_file", "review", "analyze"],
        "icon": "👁️"
    },
    "documenter": {
        "name": "Документатор",
        "description": "Пишет README, docstrings, API docs",
        "prompt": "Ты документатор. Пиши понятные README, docstrings, API документацию. Используй примеры кода. Документируй все публичные функции.",
        "allowed_tools": ["read_file", "write_file", "list_dir"],
        "icon": "📝"
    },
    "optimizer": {
        "name": "Оптимизатор",
        "description": "Улучшает производительность кода",
        "prompt": "Ты оптимизатор. Находи bottleneck'и, улучшай производительность. Профилируй код, предлагай оптимизации алгоритмов.",
        "allowed_tools": ["read_file", "write_file", "optimize", "run_code"],
        "icon": "⚡"
    },
    "security": {
        "name": "Аудитор безопасности",
        "description": "Ищет уязвимости",
        "prompt": "Ты аудитор безопасности. Ищи: SQL injection, XSS, CSRF, buffer overflows, insecure defaults. Предлагай fixes.",
        "allowed_tools": ["read_file", "review", "analyze", "shell"],
        "icon": "🛡️"
    },
    "data_analyst": {
        "name": "Аналитик данных",
        "description": "Анализирует CSV, JSON, базы данных",
        "prompt": "Ты аналитик данных. Анализируй CSV, JSON, Excel. Создавай визуализации, находи паттерны, делай выводы.",
        "allowed_tools": ["read_file", "write_file", "run_code", "database", "shell"],
        "icon": "📊"
    },
    "devops": {
        "name": "DevOps",
        "description": "Настраивает Docker, CI/CD, деплой",
        "prompt": "Ты DevOps инженер. Настраивай Dockerfile, docker-compose, CI/CD pipelines, деплой на сервера.",
        "allowed_tools": ["write_file", "shell", "read_file", "list_dir"],
        "icon": "🚀"
    },
    "scraper": {
        "name": "Скрапер",
        "description": "Собирает данные с веб-сайтов",
        "prompt": "Ты скрапер. Собирай данные с веб-сайтов через requests+BeautifulSoup или Selenium. Обходи anti-bot защиту.",
        "allowed_tools": ["web_search", "write_file", "run_code", "shell", "install"],
        "icon": "🕷️"
    },
    "custom": {
        "name": "Кастомный агент",
        "description": "Агент с пользовательской ролью",
        "prompt": "Ты кастомный агент. Определи свою роль из контекста задачи. Действуй как эксперт в этой области.",
        "allowed_tools": ["write_file", "read_file", "shell", "web_search", "run_code"],
        "icon": "🤖"
    }
}

# Все доступные инструменты
ALL_TOOLS = [
    "write_file", "read_file", "list_dir", "shell", "web_search",
    "install", "run_code", "create_tool", "spawn_agent",
    "memory_store", "memory_recall", "memory_update",
    "download", "debug", "git", "api_call", "database",
    "test_app", "analyze", "review", "document", "test",
    "optimize", "scan_project"
]


class TeamAgent:
    """Представление одного агента в команде."""
    
    def __init__(
        self,
        agent_type: str,
        name: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        agent_id: Optional[str] = None
    ):
        self.agent_id = agent_id or f"agent_{uuid.uuid4().hex[:8]}"
        self.agent_type = agent_type
        self.name = name or AGENT_TYPES.get(agent_type, {}).get("name", agent_type)
        self.custom_prompt = custom_prompt
        self.allowed_tools = allowed_tools or AGENT_TYPES.get(agent_type, {}).get("allowed_tools", [])
        
        # Статистика
        self.created_at = time.time()
        self.task_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.last_active: Optional[float] = None
    
    def get_system_prompt(self) -> str:
        """Получает системный промпт агента."""
        if self.custom_prompt:
            return self.custom_prompt
        return AGENT_TYPES.get(self.agent_type, {}).get("prompt", AGENT_TYPES["custom"]["prompt"])
    
    def get_icon(self) -> str:
        """Получает иконку агента."""
        return AGENT_TYPES.get(self.agent_type, {}).get("icon", "🤖")
    
    def has_tool(self, tool_name: str) -> bool:
        """Проверяет наличие инструмента."""
        return tool_name in self.allowed_tools
    
    def add_tool(self, tool_name: str):
        """Добавляет инструмент."""
        if tool_name in ALL_TOOLS and tool_name not in self.allowed_tools:
            self.allowed_tools.append(tool_name)
    
    def remove_tool(self, tool_name: str):
        """Удаляет инструмент."""
        if tool_name in self.allowed_tools:
            self.allowed_tools.remove(tool_name)
    
    def to_dict(self) -> Dict[str, Any]:
        """Сериализует агента в dict."""
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "name": self.name,
            "custom_prompt": self.custom_prompt,
            "allowed_tools": self.allowed_tools,
            "created_at": self.created_at,
            "task_count": self.task_count,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "last_active": self.last_active
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TeamAgent':
        """Десериализует агента из dict."""
        agent = cls(
            agent_type=data["agent_type"],
            name=data.get("name"),
            custom_prompt=data.get("custom_prompt"),
            allowed_tools=data.get("allowed_tools", []),
            agent_id=data.get("agent_id")
        )
        agent.created_at = data.get("created_at", time.time())
        agent.task_count = data.get("task_count", 0)
        agent.success_count = data.get("success_count", 0)
        agent.fail_count = data.get("fail_count", 0)
        agent.last_active = data.get("last_active")
        return agent


class Team:
    """Представление команды агентов."""
    
    def __init__(
        self,
        name: str,
        description: str = "",
        team_id: Optional[str] = None,
        strategy: str = "sequential",  # sequential, parallel, debate
        max_rounds: int = 5
    ):
        self.team_id = team_id or f"team_{uuid.uuid4().hex[:8]}"
        self.name = name
        self.description = description
        self.strategy = strategy
        self.max_rounds = max_rounds
        
        self.agents: List[TeamAgent] = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.usage_count = 0
        self.last_used: Optional[float] = None
    
    def add_agent(self, agent: TeamAgent):
        """Добавляет агента в команду."""
        self.agents.append(agent)
        self.updated_at = time.time()
    
    def remove_agent(self, agent_id: str) -> bool:
        """Удаляет агента из команды."""
        for i, agent in enumerate(self.agents):
            if agent.agent_id == agent_id:
                del self.agents[i]
                self.updated_at = time.time()
                return True
        return False
    
    def get_agent_tools(self) -> List[str]:
        """Получает все инструменты команды (объединение)."""
        tools = set()
        for agent in self.agents:
            tools.update(agent.allowed_tools)
        return sorted(list(tools))
    
    def to_dict(self) -> Dict[str, Any]:
        """Сериализует команду в dict."""
        return {
            "team_id": self.team_id,
            "name": self.name,
            "description": self.description,
            "strategy": self.strategy,
            "max_rounds": self.max_rounds,
            "agents": [agent.to_dict() for agent in self.agents],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "usage_count": self.usage_count,
            "last_used": self.last_used
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Team':
        """Десериализует команду из dict."""
        team = cls(
            name=data["name"],
            description=data.get("description", ""),
            team_id=data.get("team_id"),
            strategy=data.get("strategy", "sequential"),
            max_rounds=data.get("max_rounds", 5)
        )
        team.created_at = data.get("created_at", time.time())
        team.updated_at = data.get("updated_at", time.time())
        team.usage_count = data.get("usage_count", 0)
        team.last_used = data.get("last_used")
        
        for agent_data in data.get("agents", []):
            team.agents.append(TeamAgent.from_dict(agent_data))
        
        return team


# === TEAM MANAGEMENT FUNCTIONS ===

def create_team(
    name: str,
    description: str = "",
    strategy: str = "sequential",
    max_rounds: int = 5
) -> Team:
    """Создаёт новую команду."""
    team = Team(
        name=name,
        description=description,
        strategy=strategy,
        max_rounds=max_rounds
    )
    _save_team(team)
    logger.info(f"Team created: {name} ({team.team_id})")
    return team


def add_agent_to_team(
    team_id: str,
    agent_type: str,
    name: Optional[str] = None,
    custom_prompt: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None
) -> Optional[TeamAgent]:
    """Добавляет агента в команду."""
    team = load_team(team_id)
    if not team:
        logger.error(f"Team not found: {team_id}")
        return None
    
    agent = TeamAgent(
        agent_type=agent_type,
        name=name,
        custom_prompt=custom_prompt,
        allowed_tools=allowed_tools
    )
    team.add_agent(agent)
    _save_team(team)
    
    logger.info(f"Agent added to team {team_id}: {agent.name} ({agent.agent_id})")
    return agent


def remove_agent_from_team(team_id: str, agent_id: str) -> bool:
    """Удаляет агента из команды."""
    team = load_team(team_id)
    if not team:
        return False
    
    result = team.remove_agent(agent_id)
    if result:
        _save_team(team)
        logger.info(f"Agent removed from team {team_id}: {agent_id}")
    
    return result


def load_team(team_id: str) -> Optional[Team]:
    """Загружает команду по ID."""
    safe_id = team_id.replace("/", "").replace("\\", "")
    path = os.path.join(TEAMS_DIR, f"{safe_id}.json")
    
    if not os.path.exists(path):
        return None
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Team.from_dict(data)
    except Exception as e:
        logger.error(f"Failed to load team: {e}")
        return None


def list_teams() -> List[Dict[str, Any]]:
    """Список всех команд."""
    teams = []
    
    if not os.path.exists(TEAMS_DIR):
        return teams
    
    for filename in os.listdir(TEAMS_DIR):
        if not filename.endswith(".json"):
            continue
        
        path = os.path.join(TEAMS_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            team = Team.from_dict(data)
            teams.append({
                "team_id": team.team_id,
                "name": team.name,
                "description": team.description,
                "strategy": team.strategy,
                "max_rounds": team.max_rounds,
                "agent_count": len(team.agents),
                "agent_types": [a.agent_type for a in team.agents],
                "created_at": team.created_at,
                "updated_at": team.updated_at,
                "usage_count": team.usage_count,
                "last_used": team.last_used
            })
        except Exception as e:
            logger.warning(f"Failed to load team {filename}: {e}")
            continue
    
    return sorted(teams, key=lambda x: x.get("updated_at", 0), reverse=True)


def delete_team(team_id: str) -> bool:
    """Удаляет команду."""
    safe_id = team_id.replace("/", "").replace("\\", "")
    path = os.path.join(TEAMS_DIR, f"{safe_id}.json")
    
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.info(f"Team deleted: {team_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete team: {e}")
            return False
    
    return False


def _save_team(team: Team):
    """Сохраняет команду."""
    safe_id = team.team_id.replace("/", "").replace("\\", "")
    path = os.path.join(TEAMS_DIR, f"{safe_id}.json")
    
    team.updated_at = time.time()
    
    with open(path, "w", encoding="utf-8") as f:
        json.dump(team.to_dict(), f, ensure_ascii=False, indent=2)


def get_team_with_agents(team_id: str) -> Optional[Dict[str, Any]]:
    """Получает команду с полной информацией об агентах."""
    team = load_team(team_id)
    if not team:
        return None
    
    team.usage_count += 1
    team.last_used = time.time()
    _save_team(team)
    
    return {
        "team": team.to_dict(),
        "agents": [agent.to_dict() for agent in team.agents],
        "all_tools": team.get_agent_tools(),
        "agent_count": len(team.agents)
    }


def update_team_agent_tools(
    team_id: str,
    agent_id: str,
    allowed_tools: List[str]
) -> bool:
    """Обновляет инструменты агента в команде."""
    team = load_team(team_id)
    if not team:
        return False
    
    for agent in team.agents:
        if agent.agent_id == agent_id:
            agent.allowed_tools = [t for t in allowed_tools if t in ALL_TOOLS]
            _save_team(team)
            return True
    
    return False


def create_team_from_config(config: Dict[str, Any]) -> Optional[Team]:
    """Создаёт команду из конфигурации."""
    try:
        team = Team(
            name=config.get("name", "Новая команда"),
            description=config.get("description", ""),
            strategy=config.get("strategy", "sequential"),
            max_rounds=config.get("max_rounds", 5)
        )
        
        for agent_config in config.get("agents", []):
            agent = TeamAgent(
                agent_type=agent_config.get("agent_type", "custom"),
                name=agent_config.get("name"),
                custom_prompt=agent_config.get("custom_prompt"),
                allowed_tools=agent_config.get("allowed_tools")
            )
            team.add_agent(agent)
        
        _save_team(team)
        return team
    except Exception as e:
        logger.error(f"Failed to create team from config: {e}")
        return None


def get_team_system_prompt(team: Team, task: str) -> str:
    """Генерирует системный промпт для команды."""
    prompt_parts = []
    
    prompt_parts.append(f"""# КОМАНДНАЯ РАБОТА

Ты координатор команды. Твоя задача — распределить работу между членами команды.

## Команда: {team.name}
{team.description}

## Стратегия: {team.strategy}
## Максимум раундов: {team.max_rounds}

## Участники команды:""")
    
    for agent in team.agents:
        prompt_parts.append(f"""
### {agent.get_icon()} {agent.name} ({agent.agent_type})
- **ID:** {agent.agent_id}
- **Инструменты:** {', '.join(agent.allowed_tools)}
- **Специализация:** {agent.get_system_prompt()[:150]}""")
    
    prompt_parts.append(f"""
## Задача: {task}

## ИНСТРУКЦИЯ:
1. Проанализируй задачу
2. Определи какие члены команды нужны
3. Распредели подзадачи между агентами
4. Каждый агент выполняет свою часть работы
5. Объедини результаты

## ФОРМАТ ОТВЕТА:
Для каждого агента используй формат:
[ACTION: delegate_to:{{agent_id}}] описание подзадачи

После выполнения всеми:
[ACTION: team_summary] общий результат""")
    
    return "\n".join(prompt_parts)
