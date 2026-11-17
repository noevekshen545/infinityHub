"""
Text-based RAG Module (fallback when FAISS unavailable)
Simple keyword-based retrieval with scoring
"""

import os
import json
import time
import re
from typing import Dict, List, Optional, Any
import swallow

# Storage directory
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent_v4")
TEXT_RAG_DIR = os.path.join(AGENT_BASE, "text_rag")
os.makedirs(TEXT_RAG_DIR, exist_ok=True)

# In-memory storage
_text_rag_data: Dict[str, List[Dict[str, Any]]] = {}


def _load_data():
    """Load text RAG data from disk."""
    global _text_rag_data
    index_file = os.path.join(TEXT_RAG_DIR, "index.json")
    if os.path.exists(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                _text_rag_data = json.load(f)
        except Exception:
            swallow.note('text_rag_module._load_data:L30')
            _text_rag_data = {}


def _save_data():
    """Save text RAG data to disk."""
    index_file = os.path.join(TEXT_RAG_DIR, "index.json")
    try:
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(_text_rag_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Text RAG save error: {e}")


def text_rag_store(session_id: str, texts: List[str], metadata: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """
    Store texts in RAG memory for a session.
    
    Args:
        session_id: Session identifier
        texts: List of texts to store
        metadata: Optional metadata for each text
    
    Returns:
        Dict with success status and count
    """
    _load_data()
    
    if session_id not in _text_rag_data:
        _text_rag_data[session_id] = []
    
    for i, text in enumerate(texts):
        meta = metadata[i] if metadata and i < len(metadata) else {"time": time.time()}
        _text_rag_data[session_id].append({
            "text": text,
            "metadata": meta,
            "timestamp": time.time(),
            "access_count": 0
        })
    
    _save_data()
    return {"success": True, "count": len(texts), "method": "text"}


def text_rag_recall(session_id: str, query: str, top_k: int = 5) -> Dict[str, Any]:
    """
    Recall relevant texts from RAG memory.
    Uses keyword matching with scoring.
    
    Args:
        session_id: Session identifier
        query: Search query
        top_k: Number of results to return
    
    Returns:
        Dict with results and scores
    """
    _load_data()
    
    texts = _text_rag_data.get(session_id, [])
    if not texts:
        return {"success": True, "results": [], "method": "text", "count": 0}
    
    # Tokenize query
    query_words = set(query.lower().split())
    
    # Score each text
    scored = []
    for item in texts:
        text_lower = item["text"].lower()
        text_words = set(text_lower.split())
        
        # Word overlap score
        overlap = len(query_words & text_words)
        score = overlap / max(1, len(query_words | text_words))
        
        # Bonus for exact phrase match
        if query.lower() in text_lower:
            score += 0.5
        
        # Bonus for recent items
        age_hours = (time.time() - item.get("timestamp", time.time())) / 3600
        recency_bonus = max(0, 1 - age_hours / 24)  # Decay over 24h
        score += recency_bonus * 0.1
        
        if score > 0:
            scored.append({
                "text": item["text"],
                "score": round(score, 3),
                "metadata": item.get("metadata", {}),
                "timestamp": item.get("timestamp", 0)
            })
    
    # Sort by score and return top_k
    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:top_k]
    
    return {"success": True, "results": results, "method": "text", "count": len(results)}


def text_rag_clear(session_id: str) -> Dict[str, Any]:
    """Clear all texts for a session."""
    _load_data()
    if session_id in _text_rag_data:
        del _text_rag_data[session_id]
        _save_data()
    return {"success": True, "cleared": True}


def text_rag_stats(session_id: str) -> Dict[str, Any]:
    """Get statistics for a session's RAG data."""
    _load_data()
    texts = _text_rag_data.get(session_id, [])
    total_chars = sum(len(t["text"]) for t in texts)
    return {
        "success": True,
        "session_id": session_id,
        "count": len(texts),
        "total_chars": total_chars,
        "method": "text"
    }
