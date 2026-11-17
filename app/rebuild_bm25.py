#!/usr/bin/env python3
"""Индексация только BM25 (без векторов)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_module import get_config
from search_engine_v2 import collect_documents, HybridSearch

CFG = get_config()
INDEX_DIR = CFG.get("index", {}).get("dir")
DATA_DIRS = CFG.get("data_dirs", [])
NOTES_DIR = CFG.get("notes_dir", os.path.expanduser("~/notes"))

print("=== СБОР ДОКУМЕНТОВ ===")
docs = collect_documents(DATA_DIRS, notes_dir=NOTES_DIR, ai_dir=None,
                         books_dir=None, zim_dirs=None, html_dirs=None)
print(f"Документов: {len(docs)}")

print("\n=== BM25 ИНДЕКСАЦИЯ ===")
hs = HybridSearch()
if hs.bm25_index:
    count = hs.bm25_index.rebuild(docs)
    print(f"BM25: {count} документов")
else:
    print("BM25 индекс не доступен")

print("\n=== ТЕСТ ПОИСКА ===")
for q in ['python', 'linux', 'docker', 'sqlite', 'веб-разработка']:
    results = hs.search(q, limit=3)
    print(f"'{q}': {len(results)} результатов")
    for i, r in enumerate(results):
        print(f"  {i+1}. {r.get('title','')[:60]} (score={r.get('hybrid_score',0):.3f})")

print("\n✅ BM25 индексация завершена!")
