"""
Team Templates Module
Готовые шаблоны команд для быстрого создания
"""

from typing import Dict, List, Any


TEAM_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "dev_team": {
        "name": "Dev Team",
        "description": "Команда для разработки приложений",
        "strategy": "sequential",
        "max_rounds": 5,
        "agents": [
            {
                "agent_type": "researcher",
                "name": "Researcher",
                "allowed_tools": ["web_search", "read_file", "write_file", "shell"]
            },
            {
                "agent_type": "coder",
                "name": "Developer",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell", "install"]
            },
            {
                "agent_type": "tester",
                "name": "QA Engineer",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell"]
            }
        ]
    },
    "research_team": {
        "name": "Research Team",
        "description": "Команда для исследования и анализа",
        "strategy": "sequential",
        "max_rounds": 3,
        "agents": [
            {
                "agent_type": "researcher",
                "name": "Lead Researcher",
                "allowed_tools": ["web_search", "read_file", "write_file", "shell"]
            },
            {
                "agent_type": "data_analyst",
                "name": "Data Analyst",
                "allowed_tools": ["read_file", "write_file", "run_code", "database", "shell"]
            },
            {
                "agent_type": "documenter",
                "name": "Technical Writer",
                "allowed_tools": ["read_file", "write_file", "list_dir"]
            }
        ]
    },
    "security_team": {
        "name": "Security Team",
        "description": "Команда для аудита безопасности",
        "strategy": "parallel",
        "max_rounds": 3,
        "agents": [
            {
                "agent_type": "security",
                "name": "Security Auditor",
                "allowed_tools": ["read_file", "review", "analyze", "shell"]
            },
            {
                "agent_type": "debugger",
                "name": "Vulnerability Analyst",
                "allowed_tools": ["read_file", "shell", "list_dir", "debug"]
            },
            {
                "agent_type": "reviewer",
                "name": "Code Reviewer",
                "allowed_tools": ["read_file", "review", "analyze"]
            }
        ]
    },
    "fullstack_team": {
        "name": "Fullstack Team",
        "description": "Полная команда для fullstack разработки",
        "strategy": "sequential",
        "max_rounds": 10,
        "agents": [
            {
                "agent_type": "architect",
                "name": "Architect",
                "allowed_tools": ["read_file", "list_dir", "scan_project", "write_file"]
            },
            {
                "agent_type": "researcher",
                "name": "Researcher",
                "allowed_tools": ["web_search", "read_file", "write_file", "shell"]
            },
            {
                "agent_type": "coder",
                "name": "Backend Developer",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell", "install"]
            },
            {
                "agent_type": "coder",
                "name": "Frontend Developer",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell"]
            },
            {
                "agent_type": "tester",
                "name": "QA Engineer",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell"]
            }
        ]
    },
    "data_team": {
        "name": "Data Science Team",
        "description": "Команда для анализа данных и ML",
        "strategy": "sequential",
        "max_rounds": 5,
        "agents": [
            {
                "agent_type": "data_analyst",
                "name": "Data Analyst",
                "allowed_tools": ["read_file", "write_file", "run_code", "database", "shell"]
            },
            {
                "agent_type": "coder",
                "name": "ML Engineer",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell", "install"]
            },
            {
                "agent_type": "documenter",
                "name": "Report Writer",
                "allowed_tools": ["read_file", "write_file", "list_dir"]
            }
        ]
    },
    "devops_team": {
        "name": "DevOps Team",
        "description": "Команда для деплоя и инфраструктуры",
        "strategy": "parallel",
        "max_rounds": 5,
        "agents": [
            {
                "agent_type": "devops",
                "name": "DevOps Engineer",
                "allowed_tools": ["write_file", "shell", "read_file", "list_dir"]
            },
            {
                "agent_type": "security",
                "name": "Security Engineer",
                "allowed_tools": ["read_file", "review", "analyze", "shell"]
            },
            {
                "agent_type": "optimizer",
                "name": "Performance Engineer",
                "allowed_tools": ["read_file", "write_file", "optimize", "run_code"]
            }
        ]
    },
    "scraper_team": {
        "name": "Web Scraping Team",
        "description": "Команда для сбора данных с веб-сайтов",
        "strategy": "sequential",
        "max_rounds": 5,
        "agents": [
            {
                "agent_type": "scraper",
                "name": "Web Scraper",
                "allowed_tools": ["web_search", "write_file", "run_code", "shell", "install"]
            },
            {
                "agent_type": "data_analyst",
                "name": "Data Cleaner",
                "allowed_tools": ["read_file", "write_file", "run_code", "database", "shell"]
            },
            {
                "agent_type": "documenter",
                "name": "Data Documenter",
                "allowed_tools": ["read_file", "write_file", "list_dir"]
            }
        ]
    },
    "single_agent": {
        "name": "Solo Agent",
        "description": "Один агент для простых задач",
        "strategy": "sequential",
        "max_rounds": 5,
        "agents": [
            {
                "agent_type": "coder",
                "name": "Assistant",
                "allowed_tools": ["write_file", "read_file", "run_code", "shell", "install", "web_search", "list_dir"]
            }
        ]
    }
}


def get_template(template_id: str) -> dict:
    """Получает шаблон по ID."""
    return TEAM_TEMPLATES.get(template_id)


def get_all_templates() -> List[Dict[str, Any]]:
    """Получает все шаблоны."""
    return [
        {
            "id": tid,
            "name": tdata["name"],
            "description": tdata["description"],
            "strategy": tdata["strategy"],
            "agent_count": len(tdata["agents"]),
            "agent_types": [a["agent_type"] for a in tdata["agents"]]
        }
        for tid, tdata in TEAM_TEMPLATES.items()
    ]


def create_team_from_template(template_id: str, team_module) -> Any:
    """Создаёт команду из шаблона."""
    template = get_template(template_id)
    if not template:
        return None
    
    return team_module.create_team_from_config(template)
