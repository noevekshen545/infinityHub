"""
Skill Marketplace Module
Обмен инструментами между пользователями
"""

import os
import json
import time
import hashlib
import logging
from typing import Dict, List, Optional, Any
import swallow

logger = logging.getLogger("infinity")

# Директория
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent")
MARKETPLACE_DIR = os.path.join(AGENT_BASE, "marketplace")
MARKETPLACE_INDEX = os.path.join(MARKETPLACE_DIR, "marketplace_index.json")
os.makedirs(MARKETPLACE_DIR, exist_ok=True)


class SkillPackage:
    """Пакет навыка."""
    
    def __init__(
        self,
        name: str,
        version: str,
        description: str,
        author: str,
        code: str,
        category: str = "utility",
        tags: List[str] = None,
        dependencies: List[str] = None,
        rating: float = 0.0,
        downloads: int = 0
    ):
        self.name = name
        self.version = version
        self.description = description
        self.author = author
        self.code = code
        self.category = category
        self.tags = tags or []
        self.dependencies = dependencies or []
        self.rating = rating
        self.downloads = downloads
        
        # Metadata
        self.package_id = self._generate_id()
        self.created_at = time.time()
        self.updated_at = time.time()
    
    def _generate_id(self) -> str:
        """Генерирует ID пакета."""
        data = f"{self.name}:{self.version}:{self.author}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "package_id": self.package_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "code": self.code,
            "category": self.category,
            "tags": self.tags,
            "dependencies": self.dependencies,
            "rating": self.rating,
            "downloads": self.downloads,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SkillPackage':
        pkg = cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            code=data["code"],
            category=data.get("category", "utility"),
            tags=data.get("tags", []),
            dependencies=data.get("dependencies", []),
            rating=data.get("rating", 0.0),
            downloads=data.get("downloads", 0)
        )
        pkg.package_id = data.get("package_id", pkg._generate_id())
        pkg.created_at = data.get("created_at", time.time())
        pkg.updated_at = data.get("updated_at", time.time())
        return pkg
    
    def save_to_file(self, directory: str = MARKETPLACE_DIR):
        """Сохраняет пакет в файл."""
        path = os.path.join(directory, f"{self.package_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return path


class SkillMarketplace:
    """Маркетплейс навыков."""
    
    def __init__(self):
        self.skills: Dict[str, SkillPackage] = {}
        self.reviews: Dict[str, List[Dict[str, Any]]] = {}
        self._load_index()
        self._load_local_skills()
    
    def _load_index(self):
        """Загружает индекс маркетплейса."""
        if os.path.exists(MARKETPLACE_INDEX):
            try:
                with open(MARKETPLACE_INDEX, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                for skill_data in data.get("skills", []):
                    skill = SkillPackage.from_dict(skill_data)
                    self.skills[skill.package_id] = skill
            except Exception as e:
                logger.error(f"Failed to load marketplace index: {e}")
    
    def _load_local_skills(self):
        """Загружает локальные навыки."""
        tools_dir = os.path.join(AGENT_BASE, "tools")
        if not os.path.exists(tools_dir):
            return
        
        for filename in os.listdir(tools_dir):
            if filename.endswith(".py"):
                filepath = os.path.join(tools_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        code = f.read()
                    
                    # Ищем метаданные в комментариях
                    name = filename.replace(".py", "")
                    description = ""
                    
                    for line in code.split("\n")[:10]:
                        if line.startswith("# Description:"):
                            description = line.split(":", 1)[1].strip()
                        elif line.startswith("# Name:"):
                            name = line.split(":", 1)[1].strip()
                    
                    skill = SkillPackage(
                        name=name,
                        version="1.0.0",
                        description=description or f"Local tool: {name}",
                        author="local",
                        code=code,
                        category="local"
                    )
                    
                    self.skills[skill.package_id] = skill
                except Exception:
                    swallow.note('skill_marketplace._load_local_skills:L159')
                    pass
    
    def publish_skill(self, skill: SkillPackage) -> str:
        """Публикует навык в маркетплейс."""
        # Проверяем нет ли уже такого
        for existing in self.skills.values():
            if existing.name == skill.name and existing.author == skill.author:
                return f"Skill already exists: {existing.package_id}"
        
        self.skills[skill.package_id] = skill
        skill.save_to_file()
        self._save_index()
        
        logger.info(f"Skill published: {skill.name} v{skill.version}")
        return skill.package_id
    
    def search_skills(
        self,
        query: str = "",
        category: str = "",
        min_rating: float = 0.0,
        tags: List[str] = None
    ) -> List[Dict[str, Any]]:
        """Ищет навыки."""
        results = []
        
        for skill in self.skills.values():
            score = 0
            
            # Search query
            if query:
                query_lower = query.lower()
                if query_lower in skill.name.lower():
                    score += 5
                if query_lower in skill.description.lower():
                    score += 3
                if any(query_lower in tag.lower() for tag in skill.tags):
                    score += 2
            
            # Filter by category
            if category and skill.category != category:
                continue
            
            # Filter by rating
            if skill.rating < min_rating:
                continue
            
            # Filter by tags
            if tags:
                if not any(tag in skill.tags for tag in tags):
                    continue
            
            if score > 0 or not query:
                results.append({
                    **skill.to_dict(),
                    "search_score": score
                })
        
        # Sort by score and rating
        results.sort(key=lambda x: (x["search_score"], x["rating"]), reverse=True)
        return results
    
    def install_skill(self, package_id: str) -> bool:
        """Устанавливает навык."""
        if package_id not in self.skills:
            return False
        
        skill = self.skills[package_id]
        
        # Сохраняем в tools
        tools_dir = os.path.join(AGENT_BASE, "tools")
        os.makedirs(tools_dir, exist_ok=True)
        
        filepath = os.path.join(tools_dir, f"{skill.name}.py")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(skill.code)
        
        # Update downloads counter
        skill.downloads += 1
        self._save_index()
        
        logger.info(f"Skill installed: {skill.name}")
        return True
    
    def rate_skill(self, package_id: str, rating: float, review: str = "") -> bool:
        """Оценивает навык."""
        if package_id not in self.skills:
            return False
        
        if rating < 1 or rating > 5:
            return False
        
        # Add review
        if package_id not in self.reviews:
            self.reviews[package_id] = []
        
        self.reviews[package_id].append({
            "rating": rating,
            "review": review,
            "timestamp": time.time()
        })
        
        # Update average rating
        reviews = self.reviews[package_id]
        self.skills[package_id].rating = sum(r["rating"] for r in reviews) / len(reviews)
        
        self._save_index()
        return True
    
    def get_categories(self) -> List[str]:
        """Получает список категорий."""
        categories = set()
        for skill in self.skills.values():
            categories.add(skill.category)
        return sorted(list(categories))
    
    def get_popular_skills(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Получает популярные навыки."""
        skills_list = [skill.to_dict() for skill in self.skills.values()]
        skills_list.sort(key=lambda x: (x["downloads"], x["rating"]), reverse=True)
        return skills_list[:limit]
    
    def _save_index(self):
        """Сохраняет индекс."""
        try:
            data = {
                "skills": [skill.to_dict() for skill in self.skills.values()],
                "total_skills": len(self.skills),
                "last_updated": time.time()
            }
            with open(MARKETPLACE_INDEX, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save marketplace index: {e}")


# Global instance
_marketplace: Optional[SkillMarketplace] = None


def get_marketplace() -> SkillMarketplace:
    """Получает глобальный маркетплейс."""
    global _marketplace
    if _marketplace is None:
        _marketplace = SkillMarketplace()
    return _marketplace
