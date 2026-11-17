#!/usr/bin/env python3
"""Быстрая индексация: JSONL + Notes (без книг и ZIM)."""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_module import get_config
from search_engine_v2 import HybridSearch, collect_documents

CFG = get_config()
INDEX_DIR = CFG.get("index", {}).get("dir", os.path.expanduser("~/.infinity/index"))
DATA_DIRS = CFG.get("data_dirs", [])
NOTES_DIR = CFG.get("notes_dir", os.path.expanduser("~/notes"))

print("=== СБОР ДОКУМЕНТОВ (JSONL + Notes) ===")
docs = collect_documents(DATA_DIRS, notes_dir=NOTES_DIR, ai_dir=None, 
                         books_dir=None, zim_dirs=None, html_dirs=None)
print(f"Документов: {len(docs)}")

print("\n=== ИНДЕКСАЦИЯ ===")
hs = HybridSearch()
hs.rebuild_index(docs)

print("\n=== ПРОВЕРКА ===")
stats = hs.get_stats()
print(f"BM25: {stats['bm25']['engine']} ({stats['bm25']['status']})")
print(f"Vector: {stats['vector']['engine']} ({stats['vector']['status']})")
print(f"Vectors: {stats['vector']['vectors']}")

print("\n=== ТЕСТ ПОИСКА ===")
for q in ['python', 'linux', 'docker']:
    results = hs.search(q, limit=3)
    print(f"'{q}': {len(results)} результатов")
    for i, r in enumerate(results):
        print(f"  {i+1}. {r.get('title','')[:60]} (score={r.get('hybrid_score',0):.3f})")

print("\n✅ Готово!")
