"""
Human-in-the-Loop Module
Точки подтверждения в автономном режиме
"""

import os
import json
import time
import uuid
import logging
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
import swallow

logger = logging.getLogger("infinity")

# Директория
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent")
HITL_DIR = os.path.join(AGENT_BASE, "human_in_loop")
os.makedirs(HITL_DIR, exist_ok=True)


class ApprovalType(Enum):
    """Типы запросов на подтверждение."""
    DESTRUCTIVE = "destructive"  # Удаление, изменение
    INSTALL = "install"  # Установка пакетов
    NETWORK = "network"  # Сетевые запросы
    SUDO = "sudo"  # Sudo команды
    FILE_WRITE = "file_write"  # Запись в файлы
    CUSTOM = "custom"  # Пользовательский


class ApprovalRequest:
    """Запрос на подтверждение."""
    
    def __init__(
        self,
        action_type: ApprovalType,
        description: str,
        details: Dict[str, Any],
        session_id: str = "",
        auto_approve_safe: bool = True
    ):
        self.request_id = f"approval_{uuid.uuid4().hex[:8]}"
        self.action_type = action_type
        self.description = description
        self.details = details
        self.session_id = session_id
        self.auto_approve_safe = auto_approve_safe
        self.status = "pending"  # pending, approved, rejected
        self.created_at = time.time()
        self.responded_at: Optional[float] = None
        self.response_notes: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "action_type": self.action_type.value,
            "description": self.description,
            "details": self.details,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "responded_at": self.responded_at,
            "response_notes": self.response_notes
        }
    
    def save(self):
        """Сохраняет запрос."""
        path = os.path.join(HITL_DIR, f"{self.request_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


def create_approval_request(
    action_type: str,
    description: str,
    details: Dict[str, Any],
    session_id: str = "",
    auto_approve: bool = False
) -> ApprovalRequest:
    """Создаёт запрос на подтверждение."""
    req = ApprovalRequest(
        action_type=ApprovalType(action_type),
        description=description,
        details=details,
        session_id=session_id,
        auto_approve_safe=auto_approve
    )
    req.save()
    logger.info(f"[HITL] Approval request created: {req.request_id} - {description}")
    return req


def approve_request(request_id: str, notes: str = "") -> Dict[str, Any]:
    """Подтверждает запрос."""
    path = os.path.join(HITL_DIR, f"{request_id}.json")
    if not os.path.exists(path):
        return {"success": False, "error": "Request not found"}
    
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    data["status"] = "approved"
    data["responded_at"] = time.time()
    data["response_notes"] = notes
    
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"[HITL] Request approved: {request_id}")
    return {"success": True, "status": "approved"}


def reject_request(request_id: str, notes: str = "") -> Dict[str, Any]:
    """Отклоняет запрос."""
    path = os.path.join(HITL_DIR, f"{request_id}.json")
    if not os.path.exists(path):
        return {"success": False, "error": "Request not found"}
    
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    data["status"] = "rejected"
    data["responded_at"] = time.time()
    data["response_notes"] = notes
    
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"[HITL] Request rejected: {request_id}")
    return {"success": True, "status": "rejected"}


def get_pending_requests(session_id: str = "") -> List[Dict[str, Any]]:
    """Получает все ожидающие запросы."""
    requests = []
    
    for filename in os.listdir(HITL_DIR):
        if not filename.endswith(".json"):
            continue
        
        path = os.path.join(HITL_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            if data.get("status") == "pending":
                if not session_id or data.get("session_id") == session_id:
                    requests.append(data)
        except Exception:
            swallow.note('human_in_loop.get_pending_requests:L151')
            continue
    
    return sorted(requests, key=lambda x: x.get("created_at", 0), reverse=True)


def should_block_action(action_type: str, command: str = "") -> bool:
    """Проверяет нужно ли заблокировать действие (безопасность)."""
    dangerous_patterns = {
        "rm -rf /", "mkfs", "dd if=", ":(){:|:&};:",
        "chmod -R 777 /", "wget", "curl", "| bash", "| sh"
    }
    
    if any(pattern in command.lower() for pattern in dangerous_patterns):
        return True
    
    return False


def needs_approval(action_type: str, details: Dict[str, Any]) -> bool:
    """Проверяет требует ли действие подтверждения."""
    # Destructive actions всегда требуют подтверждения
    if action_type in ["destructive", "sudo"]:
        return True
    
    # File write в системные директории
    if action_type == "file_write":
        path = details.get("path", "")
        system_paths = ["/etc/", "/usr/", "/var/", "/boot/", "/sbin/"]
        if any(path.startswith(p) for p in system_paths):
            return True
    
    # Network к внешним ресурсам
    if action_type == "network":
        return True
    
    return False
