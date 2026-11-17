#!/usr/bin/env python3
"""
Скачивание fastembed-модели эмбеддингов в models/fastembed/ вручную.

Полезно, если у процесса сервера нет доступа в интернет (например, DNS в WSL
падает), а на хосте/в другой среде интернет есть. Скачайте в ту же папку
models/fastembed, которую использует сервер, — и он затем загрузит модель офлайн.

Использование:
  python3 download_fastembed_model.py
  python3 download_fastembed_model.py --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
  python3 download_fastembed_model.py --cache /путь/к/models/fastembed
"""
import os
import sys
import argparse
import json
import swallow

ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve_model_from_config():
    try:
        fp = os.path.join(ROOT, "config.json")
        if os.path.exists(fp):
            data = json.load(open(fp, encoding="utf-8"))
            dq = data.get("qdrant") or {}
            return dq.get("embed_model") or None
    except Exception:
        swallow.note('download_fastembed_model._resolve_model_from_config:L30')
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="Имя модели fastembed (по умолчанию берётся из config.json qdrant.embed_model)")
    ap.add_argument("--cache", default=None, help="Папка кэша (по умолчанию <ROOT>/models/fastembed)")
    args = ap.parse_args()

    model = args.model or _resolve_model_from_config() or "intfloat/multilingual-e5-small"
    cache = args.cache or os.path.join(ROOT, "models", "fastembed")
    os.makedirs(cache, exist_ok=True)

    print("=== Скачивание fastembed-модели ===")
    print("  Модель: %s" % model)
    print("  Кэш:    %s" % cache)

    try:
        from fastembed import TextEmbedding
    except ImportError:
        print("fastembed не установлен. Установите: pip install fastembed", file=sys.stderr)
        sys.exit(1)

    try:
        # fastembed сам качает нужный HF-репозиторий в cache_dir
        TextEmbedding(model_name=model, cache_dir=cache, local_files_only=False).embed(["проверка"])
        print("\n[OK] Модель скачана в: %s" % cache)
        print("Сервер загрузит её офлайн (local_files_only).")
    except Exception as e:
        print("\n[ERR] %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
