#!/usr/bin/env python3
"""
Скачивание модели faster-whisper в models/whisper/

Использование:
  python3 download_whisper.py              # скачать base (~140 МБ)
  python3 download_whisper.py --model small  # small (~460 МБ)
  python3 download_whisper.py --model tiny   # tiny  (~75 МБ)
  python3 download_whisper.py --model medium # medium (~1.5 ГБ)
"""
import os
import sys
import argparse
import swallow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _models_dir() -> str:
    """Та же папка, что у stt_engine: env → config speech.stt.models_dir → корень проекта."""
    d = os.environ.get("INFINITY_STT_MODELS_DIR")
    if d:
        return os.path.expanduser(d)
    try:
        from config_module import get_speech, _expand_path
        cfg = ((get_speech() or {}).get("stt") or {}).get("models_dir")
        if cfg:
            return os.path.expanduser(str(_expand_path(str(cfg))))
    except Exception:
        pass
    return os.path.join(ROOT, "models", "whisper")


MODELS_DIR = _models_dir()
os.makedirs(MODELS_DIR, exist_ok=True)

DEFAULT_MODEL = "base"

MODELS = {
    "tiny":          "Systran/faster-whisper-tiny",
    "base":          "Systran/faster-whisper-base",
    "small":         "Systran/faster-whisper-small",
    "medium":        "Systran/faster-whisper-medium",
    "large":         "Systran/faster-whisper-large-v3",
    "large-v3":      "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Альтернативы (если хочется русско-специализированную)
RU_ALTERNATIVES = {
    "base":   "gatevex/faster-whisper-base-ru",
    "small":  "gatevex/faster-whisper-small-ru",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=list(MODELS.keys()),
                    help=f"Размер модели (default: {DEFAULT_MODEL}). 'large-v3-turbo' — быстрее large-v3, размер ~1.5ГБ")
    ap.add_argument("--ru", action="store_true",
                    help="Использовать русско-специализированную модель (если есть)")
    ap.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"),
                    help="HuggingFace token (если нужен для gated моделей)")
    args = ap.parse_args()

    if args.ru and args.model in RU_ALTERNATIVES:
        model_id = RU_ALTERNATIVES[args.model]
    else:
        model_id = MODELS[args.model]

    print(f"=== Скачивание faster-whisper модели ===")
    print(f"  Модель:    {model_id}")
    print(f"  Директория: {MODELS_DIR}")
    print()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub не установлен. pip install huggingface_hub")
        sys.exit(1)

    try:
        local_path = snapshot_download(
            repo_id=model_id,
            local_dir=os.path.join(MODELS_DIR, args.model),
            token=args.hf_token,
            allow_patterns=[
                "*.bin", "*.json", "*.txt", "*.tiktoken", "tokenizer*",
                "config.json", "preprocessor_config.json", "vocabulary.*",
            ],
        )
        total = 0
        for d, _, files in os.walk(local_path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(d, f))
                except OSError:
                    swallow.note('download_whisper.main:L82')
                    pass
        size_mb = total // (1024 * 1024)
        print(f"\n[OK] Модель скачана в: {local_path}")
        print(f"  Размер: {size_mb} МБ")
        print()
        print("Запуск сервера:")
        print("  python3 server.py")
        print("  curl 'http://127.0.0.1:<порт>/api/stt/status'")
    except Exception as e:
        print(f"\n[ERR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
