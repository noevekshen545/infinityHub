#!/usr/bin/env python3
"""Отдельный процесс пересборки индексов SearchAll.

Запускается из основного сервера (search_all_sources._spawn_news_worker),
чтобы тяжёлая пересборка (например, новостей: 50k+ файлов) не блокировала
старт и поиск. Результат пишется в дисковый кэш (data/search_all_index_*.json.gz);
монитор кэша в основном процессе подхватывает его автоматически.

Использование:
    python3 search_index_worker.py --kind news
"""

import sys
import time
import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="SearchAll index worker")
    parser.add_argument("--kind", default="news", help="index kind: news|notes|ai|html")
    args = parser.parse_args()

    try:
        import search_all_sources as sa
    except Exception as e:
        print(f"[index-worker] cannot import search_all_sources: {e}", file=sys.stderr)
        return 1

    builder = sa._builder_for(args.kind)
    if builder is None:
        print(f"[index-worker] unknown kind: {args.kind}", file=sys.stderr)
        return 1

    t0 = time.time()
    try:
        items = builder() or []
        items = sa._prepare_items(args.kind, items)
        sa._save_disk_index(args.kind, items)
        sa._invalidate_results_cache()
    except Exception as e:
        print(f"[index-worker] '{args.kind}' build error: {e}", file=sys.stderr)
        return 1

    print(f"[index-worker] '{args.kind}' done: {len(items)} items in "
          f"{int((time.time() - t0) * 1000)} ms", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())