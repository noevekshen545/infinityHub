#!/usr/bin/env python3
"""
load_jobs — выполнение CPU-тяжёлых задач в ОТДЕЛЬНОМ процессе.

Запускается демоном load_daemon.py (и можно вручную):
    python load_jobs.py rebuild_search_index --force_full 0
    python load_jobs.py news_fetch --limit 20 --download_media 1
    python load_jobs.py search_all_index --kind news

Каждая задача работает в собственном процессе → собственный GIL,
поэтому индексация НЕ замораживает HTTP-сервер. Процесс запускается
с пониженным приоритетом (задаёт load_daemon).

Вывод: прогресс в stdout, финальный результат строкой "RESULT: {json}".
"""
import sys
import os
import json
import argparse
import time
import swallow

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def _emit(obj):
    print("RESULT: " + json.dumps(obj, ensure_ascii=False), flush=True)


def job_rebuild_search_index(force_full=0):
    """Ребилд гибридного поискового индекса (BM25 + FAISS/Qdrant).

    Логика повторяет server.rebuild_search_index(), но в своём процессе.
    """
    from config_module import get_config
    import search_engine_v2 as sev2
    from search_engine_v2 import (
        HybridSearch, collect_documents, incremental_rebuild
    )

    cfg = get_config()
    dirs = cfg.get("data_dirs", [])
    notes_dir = cfg.get("notes_dir", os.path.expanduser("~/notes"))
    ai_dir = cfg.get("ai_dir", os.path.join(notes_dir, "_ai_answers"))
    books_dir = cfg.get("books_dir", os.path.expanduser("~/books"))
    zim_dirs = cfg.get("zim_dirs", [])
    html_dirs = cfg.get("html_viewer_dirs", [])
    index_dir = getattr(sev2, "INDEX_DIR", None) or cfg.get("index", {}).get(
        "dir", os.path.expanduser("~/.infinity/index"))

    t0 = time.time()
    if force_full:
        print("[job] полная переиндексация: сбор документов...", flush=True)
        docs = collect_documents(dirs, notes_dir=notes_dir, ai_dir=ai_dir,
                                 books_dir=books_dir, zim_dirs=zim_dirs,
                                 html_dirs=html_dirs)
        print(f"[job] собрано {len(docs)} документов", flush=True)
        hs = HybridSearch(index_dir=index_dir)
        count = hs.rebuild_index(docs)
        result = {"action": "full_rebuild", "docs_added": int(count or 0),
                  "total_docs": len(docs)}
    else:
        print("[job] инкрементальная переиндексация...", flush=True)
        hs = HybridSearch(index_dir=index_dir)
        result = incremental_rebuild(hs, dirs, notes_dir=notes_dir,
                                     ai_dir=ai_dir, books_dir=books_dir,
                                     zim_dirs=zim_dirs, html_dirs=html_dirs)
        if not isinstance(result, dict):
            result = {"action": "incremental", "result": str(result)}
    result["elapsed_sec"] = round(time.time() - t0, 1)
    result["ok"] = True
    _emit(result)


def job_news_fetch(limit=20, download_media=1):
    """Загрузка свежих статей из всех источников (тяжёлый сетевой парсинг)."""
    import news_module
    results = news_module.fetch_all_feeds(int(limit),
                                          bool(int(download_media))) or []
    total = sum(r.get("articles", 0) for r in results if isinstance(r, dict))
    _emit({"ok": True, "sources": len(results), "articles": int(total)})


def job_search_all_index(kind="news"):
    """Пересборка индексов search-all (50k+ файлов) — уже вынесено в процесс."""
    import search_all_sources as sa
    builder = sa._builder_for(kind)
    if builder is None:
        raise SystemExit(f"unknown kind: {kind}")
    t0 = time.time()
    items = builder() or []
    items = sa._prepare_items(kind, items)
    sa._save_disk_index(kind, items)
    try:
        sa._invalidate_results_cache()
    except Exception:
        swallow.note('load_jobs.job_search_all_index:L97')
        pass
    _emit({"ok": True, "kind": kind, "items": len(items),
           "elapsed_ms": int((time.time() - t0) * 1000)})


def job_rebuild_selected(type="all", force_full=1):
    """ПОЛНАя переиндексация выбранного типа через гибридный движок
    (книги/ZIM/заметки/...). Запускается из UI (/qdrant) через load_daemon
    отдельным процессом — не блокирует сервер.

    ВНИМАНИЕ: для больших ZIM (сотни ГБ) и библиотек это часы работы.
    """
    from config_module import get_config
    from search_engine_v2 import (
        HybridSearch, _collect_books_fast, _collect_zim_fast, INDEX_DIR
    )

    cfg = get_config()
    index_dir = getattr(__import__("search_engine_v2"), "INDEX_DIR", None) \
        or cfg.get("index", {}).get("dir")
    books_dir = cfg.get("books_dir")
    zim_dirs = cfg.get("zim_dirs", [])
    notes_dir = cfg.get("notes_dir")
    ai_dir = cfg.get("ai_dir", None)

    t0 = time.time()
    docs = []
    t = type if type != "all" else "all"
    if t in ("books", "all") and books_dir:
        print(f"[job] сбор книг: {books_dir}", flush=True)
        _collect_books_fast(books_dir, docs)
        print(f"[job] книг собрано, всего доков: {len(docs)}", flush=True)
    if t in ("zim", "all") and zim_dirs:
        print(f"[job] сбор ZIM: {len(zim_dirs)} директорий", flush=True)
        _collect_zim_fast(zim_dirs, docs)
        print(f"[job] всего доков: {len(docs)}", flush=True)

    if not docs and t not in ("all",):
        result = {"ok": False, "error": f"нет данных для типа '{type}' "
                  "(проверьте books_dir/zim_dirs в настройках)"}
        _emit(result)
        return

    print(f"[job] полная переиндексация {len(docs)} документов...", flush=True)
    hs = HybridSearch(index_dir=index_dir)
    count = hs.rebuild_index(docs)
    result = {"ok": True, "type": type, "docs_collected": len(docs),
              "docs_indexed": int(count or 0),
              "elapsed_sec": round(time.time() - t0, 1)}
    _emit(result)


JOBS = {
    "rebuild_search_index": job_rebuild_search_index,
    "news_fetch": job_news_fetch,
    "search_all_index": job_search_all_index,
    "rebuild_selected": job_rebuild_selected,
}



def main():
    parser = argparse.ArgumentParser(description="Load daemon worker")
    parser.add_argument("job", choices=sorted(JOBS.keys()))
    parser.add_argument("--force_full", default="0")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--download_media", default="1")
    parser.add_argument("--kind", default="news")
    parser.add_argument("--type", dest="type", default="all")
    args = parser.parse_args()
    fn = JOBS[args.job]
    if args.job == "rebuild_search_index":
        fn(force_full=int(args.force_full))
    elif args.job == "news_fetch":
        fn(limit=int(args.limit), download_media=args.download_media)
    elif args.job == "search_all_index":
        fn(kind=args.kind)
    elif args.job == "rebuild_selected":
        fn(type=args.type)
    return 0


if __name__ == "__main__":
    sys.exit(main())
