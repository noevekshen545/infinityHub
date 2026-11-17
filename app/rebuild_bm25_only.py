#!/usr/bin/env python3
"""Быстрая индексация только BM25 (Tantivy) без векторов."""

import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_module import get_config
CFG = get_config()

INDEX_DIR = CFG.get("index", {}).get("dir", os.path.expanduser("~/.infinity/index"))
DATA_DIRS = CFG.get("data_dirs", [])
NOTES_DIR = CFG.get("notes_dir", os.path.expanduser("~/notes"))
AI_DIR = CFG.get("ai_dir", os.path.join(NOTES_DIR, "_ai_answers"))
BOOKS_DIR = CFG.get("books_dir", CFG.get("books_dir", os.path.expanduser("~/books")))
ZIM_DIRS = CFG.get("zim_dirs", [])
HTML_DIRS = CFG.get("html_viewer_dirs", [])

print("=" * 60)
print("БЫСТРАЯ ИНДЕКСАЦИЯ BM25 (Tantivy)")
print("=" * 60)
print(f"INDEX_DIR: {INDEX_DIR}")
print(f"DATA_DIRS: {DATA_DIRS}")

from search_engine_v2 import (
    HybridSearch, collect_documents, TANTIVY_AVAILABLE, QDRANT_AVAILABLE
)

print(f"\nTantivy: {TANTIVY_AVAILABLE}, Qdrant: {QDRANT_AVAILABLE}")

# Сбор документов
print("\n1. Сбор документов...")
start = time.time()
docs = collect_documents(DATA_DIRS, notes_dir=NOTES_DIR, ai_dir=AI_DIR,
                         books_dir=BOOKS_DIR, zim_dirs=ZIM_DIRS, html_dirs=HTML_DIRS)
print(f"   Найдено: {len(docs)} документов за {time.time()-start:.1f} сек")

# Статистика по типам
types = {}
for d in docs:
    t = d.get('extra', {}).get('type', 'unknown')
    types[t] = types.get(t, 0) + 1
print(f"   Типы: {types}")

# Создание HybridSearch
print("\n2. Создание HybridSearch...")
hs = HybridSearch()
print(f"   BM25: {type(hs.bm25_index).__name__ if hs.bm25_index else 'none'}")
print(f"   Vector: {type(hs.vector_index).__name__ if hs.vector_index else 'none'}")

# Индексация BM25
print(f"\n3. Индексация BM25...")
start = time.time()
if hs.bm25_index:
    count = hs.bm25_index.rebuild(docs)
    print(f"   BM25: {count} документов за {time.time()-start:.1f} сек")
else:
    print("   BM25 индекс не доступен")

# Статистика
print(f"\n4. Проверка...")
stats = hs.get_stats()
print(f"   BM25: {stats['bm25']['engine']} ({stats['bm25']['status']})")
print(f"   Vector: {stats['vector']['engine']} ({stats['vector']['status']})")
print(f"   Vectors: {stats['vector']['vectors']}")

# Тест поиска
print(f"\n5. Тест поиска...")
for query in ['python', 'linux', 'docker']:
    results = hs.search(query, limit=2)
    print(f"   '{query}': {len(results)} результатов")
    for i, r in enumerate(results):
        print(f"     {i+1}. {r.get('title', '')[:60]} (score={r.get('hybrid_score', 0):.3f})")

print(f"\n✅ BM25 индексация завершена!")
print(f"   Для векторного поиска запустите: python rebuild_vectors.py")
