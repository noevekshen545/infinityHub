"""
Agent Analytics Dashboard Module
Статистика и аналитика использования агента
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional, Any
from collections import defaultdict
import swallow

logger = logging.getLogger("infinity")

# Директория (consistent with v4)
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
ANALYTICS_DIR = os.path.join(AGENT_BASE, "analytics")
ANALYTICS_FILE = os.path.join(ANALYTICS_DIR, "analytics.json")
os.makedirs(ANALYTICS_DIR, exist_ok=True)


class AgentAnalytics:
    """Аналитика агента."""
    
    def __init__(self):
        self.data = self._load_data()
    
    def _load_data(self) -> Dict[str, Any]:
        """Загружает данные аналитики."""
        if os.path.exists(ANALYTICS_FILE):
            try:
                with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                swallow.note('agent_analytics._load_data:L35')
                pass
        
        return {
            "created_at": time.time(),
            "total_sessions": 0,
            "total_messages": 0,
            "total_actions": 0,
            "total_llm_calls": 0,
            "success_rate": 0.0,
            "avg_response_time": 0.0,
            "actions_by_type": {},
            "sessions_by_mode": {},
            "daily_stats": {},
            "hourly_heatmap": {},
            "top_errors": [],
            "top_actions": [],
            "provider_stats": {}
        }
    
    def record_session(self, session_id: str, mode: str, message_count: int, duration: float):
        """Записывает сессию."""
        self.data["total_sessions"] += 1
        self.data["total_messages"] += message_count
        
        if mode not in self.data["sessions_by_mode"]:
            self.data["sessions_by_mode"][mode] = 0
        self.data["sessions_by_mode"][mode] += 1
        
        # Daily stats
        day = time.strftime("%Y-%m-%d")
        if day not in self.data["daily_stats"]:
            self.data["daily_stats"][day] = {
                "sessions": 0,
                "messages": 0,
                "actions": 0,
                "llm_calls": 0
            }
        self.data["daily_stats"][day]["sessions"] += 1
        self.data["daily_stats"][day]["messages"] += message_count
        
        # Hourly heatmap
        hour = time.strftime("%H")
        if hour not in self.data["hourly_heatmap"]:
            self.data["hourly_heatmap"][hour] = 0
        self.data["hourly_heatmap"][hour] += 1
        
        self._save_data()
    
    def record_action(self, action_type: str, success: bool, duration: float = 0.0):
        """Записывает действие."""
        self.data["total_actions"] += 1
        
        if action_type not in self.data["actions_by_type"]:
            self.data["actions_by_type"][action_type] = {"total": 0, "success": 0, "failed": 0}
        
        self.data["actions_by_type"][action_type]["total"] += 1
        if success:
            self.data["actions_by_type"][action_type]["success"] += 1
        else:
            self.data["actions_by_type"][action_type]["failed"] += 1
        
        # Update success rate
        total = self.data["total_actions"]
        success_count = sum(a["success"] for a in self.data["actions_by_type"].values())
        self.data["success_rate"] = round(success_count / max(1, total) * 100, 2)
        
        # Daily stats
        day = time.strftime("%Y-%m-%d")
        if day in self.data["daily_stats"]:
            self.data["daily_stats"][day]["actions"] += 1
        
        self._save_data()
    
    def record_llm_call(self, provider: str, tokens: int, latency: float, success: bool):
        """Записывает вызов LLM."""
        self.data["total_llm_calls"] += 1
        
        if provider not in self.data["provider_stats"]:
            self.data["provider_stats"][provider] = {
                "calls": 0,
                "tokens": 0,
                "avg_latency": 0.0,
                "errors": 0
            }
        
        stats = self.data["provider_stats"][provider]
        stats["calls"] += 1
        stats["tokens"] += tokens
        
        # Update avg latency
        stats["avg_latency"] = round(
            (stats["avg_latency"] * (stats["calls"] - 1) + latency) / stats["calls"],
            2
        )
        
        if not success:
            stats["errors"] += 1
        
        # Daily stats
        day = time.strftime("%Y-%m-%d")
        if day in self.data["daily_stats"]:
            self.data["daily_stats"][day]["llm_calls"] += 1
        
        self._save_data()
    
    def record_error(self, error_type: str, error_message: str):
        """Записывает ошибку."""
        error_entry = {
            "type": error_type,
            "message": error_message[:200],
            "timestamp": time.time()
        }
        
        self.data["top_errors"].append(error_entry)
        self.data["top_errors"] = self.data["top_errors"][-50:]  # Keep last 50
        
        self._save_data()
    
    def get_dashboard(self) -> Dict[str, Any]:
        """Получает данные для dashboard."""
        # Calculated metrics
        total_sessions = self.data["total_sessions"]
        total_actions = self.data["total_actions"]
        total_llm_calls = self.data["total_llm_calls"]
        
        # Action success rates
        action_rates = {}
        for action_type, stats in self.data["actions_by_type"].items():
            action_rates[action_type] = {
                "total": stats["total"],
                "success_rate": round(stats["success"] / max(1, stats["total"]) * 100, 2)
            }
        
        # Daily trend (last 7 days)
        daily_trend = []
        for i in range(6, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            if day in self.data["daily_stats"]:
                daily_trend.append({
                    "date": day,
                    **self.data["daily_stats"][day]
                })
            else:
                daily_trend.append({
                    "date": day,
                    "sessions": 0,
                    "messages": 0,
                    "actions": 0,
                    "llm_calls": 0
                })
        
        return {
            "overview": {
                "total_sessions": total_sessions,
                "total_messages": self.data["total_messages"],
                "total_actions": total_actions,
                "total_llm_calls": total_llm_calls,
                "success_rate": self.data["success_rate"],
                "uptime_hours": round((time.time() - self.data["created_at"]) / 3600, 2)
            },
            "actions_by_type": action_rates,
            "sessions_by_mode": self.data["sessions_by_mode"],
            "daily_trend": daily_trend,
            "hourly_heatmap": self.data["hourly_heatmap"],
            "provider_stats": self.data["provider_stats"],
            "top_errors": self.data["top_errors"][-10:],
            "last_updated": time.time()
        }
    
    def _save_data(self):
        """Сохраняет данные."""
        try:
            with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save analytics: {e}")


# Global instance
_analytics: Optional[AgentAnalytics] = None


def get_analytics() -> AgentAnalytics:
    """Получает глобальный экземпляр."""
    global _analytics
    if _analytics is None:
        _analytics = AgentAnalytics()
    return _analytics


def record_action_wrapper(action_type: str, success: bool, duration: float = 0.0):
    """Wrapper для записи действий."""
    try:
        get_analytics().record_action(action_type, success, duration)
    except Exception as e:
        logger.debug(f"Analytics record failed: {e}")


def record_session_wrapper(session_id: str, mode: str, message_count: int, duration: float):
    """Wrapper для записи сессий."""
    try:
        get_analytics().record_session(session_id, mode, message_count, duration)
    except Exception as e:
        logger.debug(f"Analytics record failed: {e}")
