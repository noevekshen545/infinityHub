#!/usr/bin/env python3
"""
Скрипт полной переиндексации с правильным dimension.
Удаляет старые индексы и создаёт новые с Jina v4 (dim=1024).
"""

import os
import sys
import time

# Добавляем путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_module import get_config
CFG = get_config()

print("=" * 60)
print("ПОЛНАЯ ПЕРЕИНДЕКСАЦИЯ")
print("=" * 60)

# Пути
INDEX_DIR = CFG.get("index", {}).get("dir", os.path.expanduser("~/.infinity/index"))
DATA_DIRS = CFG.get("data_dirs", [])
NOTES_DIR = CFG.get("notes_dir", os.path.expanduser("~/notes"))
AI_DIR = CFG.get("ai_dir", os.path.join(NOTES_DIR, "_ai_answers"))
BOOKS_DIR = CFG.get("books_dir", os.path.expanduser("~/books"))
ZIM_DIRS = CFG.get("zim_dirs", [])
HTML_DIRS = CFG.get("html_viewer_dirs", [])
DIMENSION = CFG.get("index", {}).get("dimension", 1024)

print(f"\nINDEX_DIR: {INDEX_DIR}")
print(f"DATA_DIRS: {DATA_DIRS}")
print(f"DIMENSION: {DIMENSION}")

# 1. Убираем старые индексы в бэкап (не удаляем безвозвратно)
print(f"\n1. Бэкап старых индексов...")
BACKUP_DIR = None
if os.path.exists(INDEX_DIR):
    BACKUP_DIR = f"{INDEX_DIR}.backup-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(INDEX_DIR, BACKUP_DIR)
        print(f"   Старый индекс перемещён: {BACKUP_DIR}")
    except OSError as e:
        print(f"   НЕ УДАЛОСЬ переместить индекс: {e}")
        print("   Остановите InfinitySearch (файлы индекса заняты) и повторите.")
        sys.exit(1)
print("   Готово!")

# 2. Импортируем поисковый движок
print(f"\n2. Загрузка поискового движка...")
from search_engine_v2 import (
    HybridSearch, collect_documents, DIMENSION as ACTUAL_DIM
)
print(f"   ACTUAL_DIM: {ACTUAL_DIM}")

# 3. Собираем документы
print(f"\n3. Сбор документов...")
docs = collect_documents(DATA_DIRS, notes_dir=NOTES_DIR, ai_dir=AI_DIR, 
                         books_dir=BOOKS_DIR, zim_dirs=ZIM_DIRS, html_dirs=HTML_DIRS)
print(f"   Найдено: {len(docs)} документов")

# 4. Создаём HybridSearch
print(f"\n4. Создание HybridSearch...")
hs = HybridSearch(index_dir=INDEX_DIR)
print(f"   BM25: {type(hs.bm25_index).__name__}")
print(f"   Vector: {type(hs.vector_index).__name__} (dim={hs.vector_index.dimension})")

# 5. Переиндексация
print(f"\n5. Переиндексация...")
start = time.time()
hs.rebuild_index(docs)
elapsed = time.time() - start
print(f"   Готово за {elapsed:.1f} сек")

# 6. Проверка
print(f"\n6. Проверка...")
print(f"   BM25 docs: {hs.bm25_index.index_size() if hasattr(hs.bm25_index, 'index_size') else 'N/A'}")
print(f"   Vector docs: {hs.vector_index.ntotal}")

# Тестовый поиск
results = hs.search("test", limit=3)
print(f"   Test search: {len(results)} результатов")
for i, r in enumerate(results):
    print(f"     {i+1}. {r.get('title', 'N/A')[:60]} (score={r.get('hybrid_score', 0):.3f})")

print(f"\n✅ Переиндексация завершена!")
print(f"   Векторов: {hs.vector_index.ntotal}")
print(f"   Dimension: {hs.vector_index.dimension}")
