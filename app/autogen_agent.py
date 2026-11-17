#!/usr/bin/env python3
"""
A.S.I.O. Agent v3.0 - Microsoft AutoGen Integration
Полноценная мульти-агентная система с AutoGen
"""

import os
import json
import time
import uuid
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path
import swallow

logger = logging.getLogger("infinity")

# Directories
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_autogen")
MEMORY_DIR = os.path.join(AGENT_BASE, "memory")
TOOLS_DIR = os.path.join(AGENT_BASE, "tools")
SESSIONS_DIR = os.path.join(AGENT_BASE, "sessions")
WORKSPACES_DIR = os.path.join(AGENT_BASE, "workspaces")
LOGS_DIR = os.path.join(AGENT_BASE, "logs")

for d in [AGENT_BASE, MEMORY_DIR, TOOLS_DIR, SESSIONS_DIR, WORKSPACES_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)

# Try to import autogen
AUTOGEN_AVAILABLE = False
try:
    import autogen
    from autogen import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager
    AUTOGEN_AVAILABLE = True
    logger.info("✅ AutoGen imported successfully")
except ImportError:
    logger.warning("⚠️ AutoGen not installed. Install: pip install pyautogen")


# === CONFIG ===

def get_autogen_config():
    """Получает конфигурацию AutoGen"""
    from config_module import get_config
    cfg = get_config()
    
    provider = cfg.get("llm_provider", {})
    provider_type = provider.get("type", "local")
    
    if provider_type == "openai_compatible":
        openai_cfg = provider.get("openai_compatible", {})
        return {
            "config_list": [{
                "model": openai_cfg.get("model", "gpt-4"),
                "api_key": openai_cfg.get("api_key", ""),
                "base_url": openai_cfg.get("base_url", "https://api.openai.com/v1"),
            }],
            "temperature": openai_cfg.get("temperature", 0.7),
            "timeout": openai_cfg.get("timeout", 300),
            "cache_seed": None,  # Disable caching for dynamic responses
            "provider": "openai_compatible"
        }
    else:
        # Local LM Studio
        local_cfg = provider.get("local", {})
        return {
            "config_list": [{
                "model": local_cfg.get("model", "gemma-4-e2b-it"),
                "api_key": local_cfg.get("api_key", "not-needed"),
                "base_url": f"{local_cfg.get('url', 'http://localhost:1234').strip()}/v1",
            }],
            "temperature": 0.7,
            "timeout": 120,
            "cache_seed": None,
            "provider": "local"
        }


# === AGENT TYPES ===

AGENT_ROLES = {
    "coder": {
        "name": "💻 Coder",
        "system_message": """Ты expert программист. Пиши чистый, эффективный, хорошо документированный код.
Правила:
- Всегда добавляй обработку ошибок
- Используй type hints и docstrings
- Следуй PEP 8 и best practices
- После написания кода ВСЕГДА протестируй его
- Используй только проверенные библиотеки
- Создавай файлы через write_file инструмент""",
        "description": "Пишет код на Python, JS, и других языках"
    },
    "reviewer": {
        "name": "🔍 Reviewer",
        "system_message": """Ты senior code reviewer с 15+ летним опытом.
Проверяй код на:
1. 🐛 Баги и логические ошибки
2. 🔒 Security vulnerabilities
3. ⚡ Performance issues
4. 📝 Code style и readability
5. 🏗️ Architecture и design patterns
6. 🧪 Test coverage

Для каждой проблемы укажи:
- Severity (critical/high/medium/low)
- Location (file:line)
- Description
- How to fix""",
        "description": "Проверяет код на баги, security, best practices"
    },
    "tester": {
        "name": "🧪 Tester",
        "system_message": """Ты expert по тестированию. Создавай comprehensive тесты.
Правила:
- Используй pytest
- Покрой все функции тестами
- Включи edge cases и error cases
- Добавь fixtures и mocks где нужно
- Проверяй assertions
- Запускай тесты и показывай результаты""",
        "description": "Создает и запускает unit/integration тесты"
    },
    "architect": {
        "name": "🏗️ Architect",
        "system_message": """Ты senior software architect.
Проектируй масштабируемую, поддерживаемую архитектуру.
Следуй:
- SOLID principles
- DRY и KISS
- Design patterns (Factory, Strategy, Observer, и т.д.)
- Clean Architecture
- Microservices где применимо

Описывай:
- Модули и их ответственность
- Зависимости между модулями
- API контракты
- Data flow""",
        "description": "Проектирует архитектуру проекта"
    },
    "researcher": {
        "name": "📚 Researcher",
        "system_message": """Ты исследователь-аналитик.
Твоя задача - найти и проанализировать информацию.
Используй:
- Web search для поиска информации
- Чтение документации
- Анализ best practices
- Сравнение альтернатив

Возвращай структурированный отчёт с источниками.""",
        "description": "Ищет информацию и best practices"
    },
    "documenter": {
        "name": "📝 Documenter",
        "system_message": """Ты technical writer.
Создавай:
- README.md с полным описанием проекта
- Docstrings для всех функций (Google style)
- API документацию
- Примеры использования
- Installation инструкции
- Troubleshooting guide

Документация должна быть понятна даже новичкам.""",
        "description": "Пишет документацию и README"
    },
    "optimizer": {
        "name": "⚡ Optimizer",
        "system_message": """Ты expert по оптимизации производительности.
Анализируй и улучшай:
- Time complexity алгоритмов
- Memory usage
- I/O операции
- Database queries
- Network requests

Предлагай конкретные оптимизации с benchmark результатами.""",
        "description": "Оптимизирует производительность кода"
    },
    "debugger": {
        "name": "🐛 Debugger",
        "system_message": """Ты expert по отладке.
Анализируй:
- Stack traces
- Error messages
- Logs
- Runtime behavior

Находи root cause и предлагай конкретные исправления.
Используй систематический подход к debugging.""",
        "description": "Отлаживает ошибки и находит причины"
    }
}


# === AUTOGEN SESSION ===

class AutoGenSession:
    """Управление AutoGen сессией с мульти-агентной системой"""
    
    def __init__(self, session_id: str, workspace: str = None):
        self.session_id = session_id
        self.workspace = workspace or os.path.join(WORKSPACES_DIR, session_id)
        os.makedirs(self.workspace, exist_ok=True)
        
        self.agents = {}
        self.group_chat = None
        self.manager = None
        self.messages = []
        self.started_at = time.time()
        
    def create_agents(self, roles: List[str] = None, llm_config: Dict = None):
        """Создаёт агентов указанных ролей"""
        if not AUTOGEN_AVAILABLE:
            return {"error": "AutoGen не установлен. pip install pyautogen"}
        
        if not llm_config:
            llm_config = get_autogen_config()
        
        roles = roles or ["coder", "reviewer", "tester"]
        
        # Создаём user proxy agent (для выполнения кода)
        user_proxy = UserProxyAgent(
            name="👤 User",
            system_message="User proxy. Выполняет код и показывает результаты.",
            code_execution_config={
                "work_dir": self.workspace,
                "use_docker": False,
            },
            human_input_mode="NEVER",  # Full autonomy
            max_consecutive_auto_reply=10,
        )
        self.agents["user"] = user_proxy
        
        # Создаём agents по ролям
        for role in roles:
            if role in AGENT_ROLES:
                role_info = AGENT_ROLES[role]
                agent = AssistantAgent(
                    name=role_info["name"],
                    system_message=role_info["system_message"],
                    llm_config=llm_config,
                )
                self.agents[role] = agent
                logger.info(f"✅ Created agent: {role_info['name']}")
        
        # Создаём Group Chat
        agents_list = [user_proxy] + list(self.agents.values())
        
        self.group_chat = GroupChat(
            agents=agents_list,
            messages=[],
            max_round=20,
            speaker_selection_method="auto",  # Auto-select who speaks next
            allow_repeat_speaker=True,
        )
        
        # Создаём Group Chat Manager
        self.manager = GroupChatManager(
            groupchat=self.group_chat,
            llm_config=llm_config,
        )
        
        return {
            "success": True,
            "agents": [role for role in self.agents.keys()],
            "workspace": self.workspace
        }
    
    def initiate_chat(self, task: str) -> Dict:
        """Запускает мульти-агентный чат"""
        if not self.manager:
            return {"error": "Агенты не созданы. Сначала вызовите create_agents()"}
        
        user_proxy = self.agents.get("user")
        if not user_proxy:
            return {"error": "User proxy не создан"}
        
        try:
            logger.info(f"[AUTOGEN] Starting chat: {task[:100]}...")
            
            # Initiate chat
            result = user_proxy.initiate_chat(
                self.manager,
                message=task,
            )
            
            # Extract conversation
            conversation = []
            for msg in self.group_chat.messages:
                conversation.append({
                    "role": msg.get("name", "unknown"),
                    "content": msg.get("content", ""),
                    "time": time.time()
                })
            
            # Save session
            self._save_session(conversation)
            
            return {
                "success": True,
                "conversation": conversation,
                "summary": result.summary if hasattr(result, 'summary') else "",
                "cost": result.cost if hasattr(result, 'cost') else 0,
                "messages_count": len(conversation)
            }
            
        except Exception as e:
            logger.error(f"[AUTOGEN] Chat error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    def _save_session(self, conversation: List[Dict]):
        """Сохраняет сессию"""
        session_data = {
            "id": self.session_id,
            "workspace": self.workspace,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "messages": conversation,
            "agents": list(self.agents.keys())
        }
        
        session_file = os.path.join(SESSIONS_DIR, f"{self.session_id}.json")
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
    
    @staticmethod
    def list_sessions() -> List[Dict]:
        """Список всех сессий"""
        sessions = []
        for fn in os.listdir(SESSIONS_DIR):
            if fn.endswith(".json"):
                fp = os.path.join(SESSIONS_DIR, fn)
                try:
                    with open(fp, "r") as f:
                        data = json.load(f)
                    sessions.append({
                        "id": data["id"],
                        "agents": data.get("agents", []),
                        "messages_count": len(data.get("messages", [])),
                        "updated_at": data.get("updated_at", 0),
                        "workspace": data.get("workspace", "")
                    })
                except Exception:
                    swallow.note('autogen_agent.list_sessions:L346')
                    pass
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return sessions
    
    @staticmethod
    def load_session(session_id: str) -> Optional[Dict]:
        """Загружает сессию"""
        fp = os.path.join(SESSIONS_DIR, f"{session_id}.json")
        if os.path.exists(fp):
            with open(fp, "r") as f:
                return json.load(f)
        return None
    
    @staticmethod
    def delete_session(session_id: str) -> bool:
        """Удаляет сессию"""
        fp = os.path.join(SESSIONS_DIR, f"{session_id}.json")
        if os.path.exists(fp):
            os.remove(fp)
            # Удаляем workspace
            ws = os.path.join(WORKSPACES_DIR, session_id)
            if os.path.exists(ws):
                import shutil
                shutil.rmtree(ws, ignore_errors=True)
            return True
        return False


# === LEGACY COMPATIBILITY ===

def process_message_legacy(session_id: str, message: str, **kwargs):
    """
    Legacy wrapper для совместимости со старым API.
    Использует простого assistant agent без group chat.
    """
    if not AUTOGEN_AVAILABLE:
        # Fallback к старому agent_module
        from agent_module import process_message
        return process_message(session_id, message, **kwargs)
    
    llm_config = get_autogen_config()
    
    # Создаём простого агента
    assistant = AssistantAgent(
        name="A.S.I.O. Assistant",
        system_message="""Ты A.S.I.O. - автономный агент.
Выполняй задачи самостоятельно, используя инструменты.
Пиши код, создавай файлы, тестируй результаты.""",
        llm_config=llm_config,
    )
    
    user_proxy = UserProxyAgent(
        name="User",
        system_message="User proxy для выполнения кода",
        code_execution_config={
            "work_dir": os.path.join(WORKSPACES_DIR, session_id),
            "use_docker": False,
        },
        human_input_mode="NEVER",
    )
    
    try:
        result = user_proxy.initiate_chat(
            assistant,
            message=message,
            max_turns=10,
        )
        
        return {
            "ok": True,
            "answer": result.summary if hasattr(result, 'summary') else "",
            "conversation": result.chat_history if hasattr(result, 'chat_history') else [],
        }
    except Exception as e:
        swallow.note('autogen_agent.process_message_legacy:L421', e)
        return {"error": str(e)}


# === INIT ===

if AUTOGEN_AVAILABLE:
    logger.info(f"✅ AutoGen Agent v3.0 initialized")
    logger.info(f"   Agents: {', '.join(AGENT_ROLES.keys())}")
    logger.info(f"   Workspace: {WORKSPACES_DIR}")
else:
    logger.warning("⚠️ AutoGen not available - using legacy mode")
