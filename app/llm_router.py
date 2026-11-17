"""
Multi-LLM Router Module
Автоматический выбор провайдера в зависимости от задачи
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional, Any, Callable
from enum import Enum

logger = logging.getLogger("infinity")


class TaskComplexity(Enum):
    """Сложность задачи."""
    SIMPLE = "simple"  # Простые вопросы, суммаризация
    MEDIUM = "medium"  # Код, анализ
    COMPLEX = "complex"  # Сложный код, креатив
    EXPERT = "expert"  # Архитектура, research


class LLMProvider:
    """Представление LLM провайдера."""
    
    def __init__(
        self,
        name: str,
        type: str,
        url: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = "",
        max_tokens: int = 4096,
        cost_per_1m: float = 0.0,
        speed_rating: float = 1.0,
        quality_rating: float = 1.0,
        capabilities: List[str] = None
    ):
        self.name = name
        self.type = type
        self.url = url
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.cost_per_1m = cost_per_1m
        self.speed_rating = speed_rating
        self.quality_rating = quality_rating
        self.capabilities = capabilities or []
        
        # Статистика
        self.total_calls = 0
        self.total_tokens = 0
        self.total_latency = 0.0
        self.error_count = 0
        self.last_used: Optional[float] = None
    
    def get_config(self) -> Dict[str, Any]:
        """Получает конфигурацию для call_lm."""
        if self.type == "local":
            return {
                "type": "local",
                "url": self.url,
                "model": self.model,
                "api_key": self.api_key
            }
        else:
            return {
                "type": "openai_compatible",
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "max_tokens": self.max_tokens
            }
    
    def to_dict(self) -> Dict[str, Any]:
        """Сериализует провайдера."""
        return {
            "name": self.name,
            "type": self.type,
            "model": self.model,
            "cost_per_1m": self.cost_per_1m,
            "speed_rating": self.speed_rating,
            "quality_rating": self.quality_rating,
            "capabilities": self.capabilities,
            "stats": {
                "total_calls": self.total_calls,
                "total_tokens": self.total_tokens,
                "error_count": self.error_count,
                "avg_latency": self.total_latency / max(1, self.total_calls)
            }
        }


class LLMRouter:
    """Роутер для LLM провайдеров."""
    
    def __init__(self):
        self.providers: Dict[str, LLMProvider] = {}
        self.auto_select = True
        self.cost_budget: Optional[float] = None
        self.total_cost = 0.0
        self._load_config()
    
    def _load_config(self):
        """Загружает провайдеров из config.json."""
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            
            provider_cfg = cfg.get("llm_provider", {})
            
            # Local provider
            if "local" in provider_cfg:
                local = provider_cfg["local"]
                self.add_provider(LLMProvider(
                    name="Local LM Studio",
                    type="local",
                    url=local.get("url", "http://localhost:1234").strip(),
                    model=local.get("model", "gemma-4-e2b-it"),
                    api_key=local.get("api_key", ""),
                    cost_per_1m=0.0,
                    speed_rating=1.5,
                    quality_rating=3.0,
                    capabilities=["chat", "code", "analysis"]
                ))
            
            # Cloud provider
            if "openai_compatible" in provider_cfg:
                cloud = provider_cfg["openai_compatible"]
                self.add_provider(LLMProvider(
                    name="OpenRouter Cloud",
                    type="openai_compatible",
                    base_url=cloud.get("base_url", "https://openrouter.ai/api/v1"),
                    api_key=cloud.get("api_key", ""),
                    model=cloud.get("model", "minimax/minimax-m2.5:free"),
                    max_tokens=cloud.get("max_tokens", 8192),
                    cost_per_1m=0.0,
                    speed_rating=3.0,
                    quality_rating=4.0,
                    capabilities=["chat", "code", "analysis", "creative", "reasoning"]
                ))
                
        except Exception as e:
            logger.warning(f"Failed to load LLM config: {e}")
    
    def add_provider(self, provider: LLMProvider):
        """Добавляет провайдера."""
        self.providers[provider.name] = provider
        logger.info(f"LLM Provider added: {provider.name}")
    
    def remove_provider(self, name: str) -> bool:
        """Удаляет провайдера."""
        if name in self.providers:
            del self.providers[name]
            return True
        return False
    
    def classify_task(self, prompt: str) -> TaskComplexity:
        """Классифицирует сложность задачи."""
        prompt_lower = prompt.lower()
        
        expert_keywords = [
            "архитектура", "architecture", "design pattern", "микросервис",
            "оптимизация", "optimization", "производительность", "performance",
            "исследуй", "research", "сравнительный анализ", "benchmark"
        ]
        
        complex_keywords = [
            "напиши код", "write code", "создай приложение", "create app",
            "рефакторинг", "refactor", "база данных", "database",
            "API", "endpoint", "тест", "test", "отлад", "debug"
        ]
        
        medium_keywords = [
            "функци", "function", "класс", "class", "алгоритм", "algorithm",
            "анализ", "analysis", "объясни", "explain", "почему", "why"
        ]
        
        expert_score = sum(1 for kw in expert_keywords if kw in prompt_lower)
        complex_score = sum(1 for kw in complex_keywords if kw in prompt_lower)
        medium_score = sum(1 for kw in medium_keywords if kw in prompt_lower)
        
        if expert_score >= 2:
            return TaskComplexity.EXPERT
        elif complex_score >= 2:
            return TaskComplexity.COMPLEX
        elif medium_score >= 1:
            return TaskComplexity.MEDIUM
        else:
            return TaskComplexity.SIMPLE
    
    def select_provider(self, prompt: str) -> Optional[LLMProvider]:
        """Выбирает лучшего провайдера для задачи."""
        if not self.auto_select:
            return next(iter(self.providers.values()), None)
        
        complexity = self.classify_task(prompt)
        
        available = {
            name: p for name, p in self.providers.items()
            if self.cost_budget is None or self.total_cost < self.cost_budget
        }
        
        if not available:
            logger.warning("No providers available (budget exceeded)")
            return None
        
        best_provider = None
        best_score = -1
        
        for name, provider in available.items():
            score = 0
            
            if complexity == TaskComplexity.EXPERT:
                score = provider.quality_rating * 2
            elif complexity == TaskComplexity.COMPLEX:
                score = provider.quality_rating * 1.5
            elif complexity == TaskComplexity.MEDIUM:
                score = provider.quality_rating
            else:
                score = 10 - provider.speed_rating
            
            score -= provider.cost_per_1m * 0.1
            
            if score > best_score:
                best_score = score
                best_provider = provider
        
        logger.info(f"LLM Router: Selected {best_provider.name} for {complexity.value} task")
        return best_provider
    
    def smart_call(
        self,
        messages: List[Dict[str, str]],
        call_lm_func: Callable,
        temperature: float = 0.7,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Умный вызов LLM с автоматическим выбором провайдера.
        """
        prompt = messages[-1].get("content", "") if messages else ""
        provider = self.select_provider(prompt)
        
        if not provider:
            return {"success": False, "error": "No provider available"}
        
        start_time = time.time()
        try:
            config = provider.get_config()
            
            result = call_lm_func(
                messages,
                model=config.get("model"),
                lm_studio_url=config.get("url"),
                api_key=config.get("api_key"),
                temperature=temperature
            )
            
            latency = time.time() - start_time
            
            provider.total_calls += 1
            provider.total_latency += latency
            provider.last_used = time.time()
            
            token_count = len(prompt) // 4
            provider.total_tokens += token_count
            
            return {
                "success": True,
                "result": result,
                "provider": provider.name,
                "model": provider.model,
                "latency": round(latency, 2),
                "estimated_tokens": token_count,
                "cost": round(provider.cost_per_1m * token_count / 1_000_000, 6)
            }
            
        except Exception as e:
            provider.error_count += 1
            logger.error(f"LLM call failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "provider": provider.name
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """Получает статистику использования."""
        return {
            "auto_select": self.auto_select,
            "cost_budget": self.cost_budget,
            "total_cost": round(self.total_cost, 6),
            "providers": {
                name: provider.to_dict()
                for name, provider in self.providers.items()
            }
        }


# Global instance
_router: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    """Получает глобальный роутер."""
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router
