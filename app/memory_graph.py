"""
Agent Memory Graph Module
Визуализация связей между фактами и граф знаний
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from collections import defaultdict
import swallow

logger = logging.getLogger("infinity")

# Директория (consistent with agent_module v4)
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
os.makedirs(AGENT_BASE, exist_ok=True)
MEMORY_GRAPH_FILE = os.path.join(AGENT_BASE, "memory_graph.json")

# Auto-create base dir if not exists
GRAPH_DATA_DIR = os.path.join(AGENT_BASE, "graph_data")
os.makedirs(GRAPH_DATA_DIR, exist_ok=True)


class MemoryGraph:
    """Граф памяти агента."""
    
    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, Any]] = []
        self._load_graph()
    
    def _load_graph(self):
        """Загружает граф из файла.

        Исправление 30.09.2026: раньше битый JSON просто логировался, и граф
        молча оставался пустым навсегда. Теперь при повреждении файла
        поднимается резервная копия, а сам битый файл откладывается в сторону,
        чтобы следующая запись начиналась с чистого состояния.
        """
        if not os.path.exists(MEMORY_GRAPH_FILE):
            return
        data = None
        try:
            with open(MEMORY_GRAPH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load memory graph: {e}")
            data = self._load_backup(e)
        if not isinstance(data, dict):
            return
        try:
            self.nodes = data.get("nodes", {})
            self.edges = data.get("edges", [])
        except Exception as e:
            logger.error(f"Memory graph: неожиданная структура данных: {e}")

    def _load_backup(self, orig_error):
        """Пытается восстановить граф из .bak, иначе откладывает битый файл."""
        bak = MEMORY_GRAPH_FILE + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    logger.warning("Memory graph: восстановлен из резервной копии")
                    # сразу возвращаем рабочее состояние в основной файл
                    self.nodes = data.get("nodes", {})
                    self.edges = data.get("edges", [])
                    self._save_graph()
                    return data
            except Exception as e:
                logger.error(f"Memory graph: резервная копия тоже битая: {e}")
        # битый файл убираем в сторону, чтобы не мешал дальнейшей работе
        try:
            broken = f"{MEMORY_GRAPH_FILE}.corrupt-{int(time.time())}"
            os.replace(MEMORY_GRAPH_FILE, broken)
            logger.warning(f"Memory graph: повреждённый файл сохранён как {broken}, "
                           f"граф начинается с нуля ({orig_error})")
        except Exception as e:
            logger.error(f"Memory graph: не удалось отложить битый файл: {e}")
        return None
    
    def add_node(
        self,
        node_id: str,
        label: str,
        node_type: str = "fact",
        properties: Optional[Dict[str, Any]] = None,
        save: bool = True
    ):
        """Добавляет узел в граф."""
        self.nodes[node_id] = {
            "id": node_id,
            "label": label,
            "type": node_type,
            "properties": properties or {},
            "created_at": time.time(),
            "access_count": 0
        }
        if save:
            self._save_graph()
    
    def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str = "related",
        weight: float = 1.0,
        save: bool = True
    ):
        """Добавляет связь между узлами."""
        if source_id not in self.nodes or target_id not in self.nodes:
            return
        
        # Проверяем нет ли уже такой связи
        for edge in self.edges:
            if edge["source"] == source_id and edge["target"] == target_id:
                edge["weight"] = max(edge["weight"], weight)
                if save:
                    self._save_graph()
                return
        
        self.edges.append({
            "source": source_id,
            "target": target_id,
            "relation": relation,
            "weight": weight,
            "created_at": time.time()
        })
        if save:
            self._save_graph()
    
    def get_connected_nodes(self, node_id: str, max_depth: int = 2) -> List[Dict[str, Any]]:
        """Получает связанные узлы."""
        if node_id not in self.nodes:
            return []
        
        result = []
        visited: Set[str] = {node_id}
        queue = [(node_id, 0)]
        
        while queue:
            current_id, depth = queue.pop(0)
            
            if depth >= max_depth:
                continue
            
            for edge in self.edges:
                neighbor_id = None
                if edge["source"] == current_id:
                    neighbor_id = edge["target"]
                elif edge["target"] == current_id:
                    neighbor_id = edge["source"]
                
                if neighbor_id and neighbor_id not in visited:
                    visited.add(neighbor_id)
                    if neighbor_id in self.nodes:
                        node = self.nodes[neighbor_id].copy()
                        node["relation"] = edge["relation"]
                        node["distance"] = depth + 1
                        result.append(node)
                        queue.append((neighbor_id, depth + 1))
        
        return result
    
    def find_similar_nodes(self, text: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Находит похожие узлы по тексту."""
        text_lower = text.lower()
        text_words = set(text_lower.split())
        
        scored = []
        for node_id, node in self.nodes.items():
            label_lower = node["label"].lower()
            label_words = set(label_lower.split())
            
            # Word overlap
            overlap = len(text_words & label_words)
            score = overlap / max(1, len(text_words | label_words))
            
            # Properties search
            props_text = json.dumps(node.get("properties", "")).lower()
            if any(word in props_text for word in text_words):
                score += 0.3
            
            if score > 0:
                scored.append((score, node))
        
        scored.sort(key=lambda x: x[0], reverse=True)
        return [node for score, node in scored[:top_k]]
    
    def get_graph_data(self) -> Dict[str, Any]:
        """Получает данные для визуализации."""
        return {
            "nodes": [
                {
                    "id": node_id,
                    "label": node["label"],
                    "type": node["type"],
                    "size": 10 + node.get("access_count", 0) * 2
                }
                for node_id, node in self.nodes.items()
            ],
            "edges": [
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "relation": edge["relation"],
                    "weight": edge["weight"]
                }
                for edge in self.edges
            ],
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges)
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """Получает статистику графа."""
        type_counts = defaultdict(int)
        for node in self.nodes.values():
            type_counts[node["type"]] += 1
        
        relation_counts = defaultdict(int)
        for edge in self.edges:
            relation_counts[edge["relation"]] += 1
        
        return {
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "node_types": dict(type_counts),
            "relation_types": dict(relation_counts),
            "avg_connections": len(self.edges) / max(1, len(self.nodes))
        }
    
    def auto_connect_similar(self, threshold: float = 0.5, max_new_edges: int = 5000):
        """Автоматически соединяет похожие узлы.

        Оптимизировано: раньше для КАЖДОЙ пары узлов сканировался весь список
        рёбер (O(n²·E) — на тысячах узлов это часы с захватом GIL и полным
        зависанием сервера). Теперь:
          - существующие пары один раз собираются в set — O(E);
          - пары сравниваются только если узлы имеют общее слово
            (инвертированный индекс) — вместо всех n² пар;
          - max_new_edges ограничивает разрастание графа.
        Семантика (порог Jaccard по словам, связь "similar") не изменилась.
        """
        node_ids = list(self.nodes.keys())
        new_edges = 0

        # 1) Существующие пары рёбер — один проход вместо скана на каждую пару
        existing = set()
        for e in self.edges:
            existing.add((e["source"], e["target"]))
            existing.add((e["target"], e["source"]))

        # 2) Инвертированный индекс: слово -> [id узлов]
        by_word = {}
        words_of = {}
        for nid in node_ids:
            ws = frozenset(self.nodes[nid]["label"].lower().split())
            words_of[nid] = ws
            for w in ws:
                by_word.setdefault(w, []).append(nid)

        # 3) Кандидаты только среди узлов с общим словом
        for ids in by_word.values():
            if len(ids) < 2:
                continue
            for i in range(len(ids)):
                id1 = ids[i]
                w1 = words_of[id1]
                for id2 in ids[i+1:]:
                    if new_edges >= max_new_edges:
                        if new_edges:
                            logger.info(f"Auto-connected {new_edges} similar nodes (limit reached)")
                        return
                    if (id1, id2) in existing or (id2, id1) in existing:
                        continue
                    w2 = words_of[id2]
                    similarity = len(w1 & w2) / max(1, len(w1 | w2))
                    if similarity >= threshold:
                        self.add_edge(id1, id2, "similar", similarity,
                                      save=False)
                        existing.add((id1, id2))
                        existing.add((id2, id1))
                        new_edges += 1

        if new_edges > 0:
            logger.info(f"Auto-connected {new_edges} similar nodes")

    
    def _save_graph(self):
        """Сохраняет граф АТОМАРНО.

        Исправление 30.09.2026: раньше json.dump писал прямо в целевой файл,
        и при любом прерывании (kill/restart/сбой) файл оставался обрезанным —
        после этого граф не загружался никогда. Теперь пишем во временный файл
        рядом и подменяем через os.replace (атомарно на POSIX и Windows),
        плюс держим .bak предыдущего рабочего состояния.
        """
        tmp = MEMORY_GRAPH_FILE + ".tmp"
        try:
            data = {
                "nodes": self.nodes,
                "edges": self.edges,
                "last_updated": time.time()
            }
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())   # данные на диске до подмены
            # резервная копия предыдущего рабочего файла
            if os.path.exists(MEMORY_GRAPH_FILE):
                try:
                    import shutil
                    shutil.copy2(MEMORY_GRAPH_FILE, MEMORY_GRAPH_FILE + ".bak")
                except Exception as e:
                    logger.warning(f"Memory graph: не удалось обновить .bak: {e}")
            os.replace(tmp, MEMORY_GRAPH_FILE)
        except Exception as e:
            logger.error(f"Failed to save memory graph: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                swallow.note('memory_graph._save_graph:L327')
                pass


# Global instance
_graph: Optional[MemoryGraph] = None


def get_memory_graph() -> MemoryGraph:
    """Получает глобальный граф."""
    global _graph
    if _graph is None:
        _graph = MemoryGraph()
    return _graph


def extract_knowledge_to_graph(knowledge_text: str):
    """Извлекает знания из текста в граф."""
    import re
    
    graph = get_memory_graph()
    
    # Простой парсинг фактов (можно улучшить с NLP)
    sentences = re.split(r'[.!?]+', knowledge_text)
    
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 10:
            continue
        
        # Создаём узел
        node_id = f"fact_{abs(hash(sentence)) % 1000000}"
        graph.add_node(node_id, sentence[:100], "fact", {"full_text": sentence})
        
        # Ищем связи с существующими узлами
        similar = graph.find_similar_nodes(sentence, top_k=3)
        for similar_node in similar:
            graph.add_edge(node_id, similar_node["id"], "related", 0.7)
