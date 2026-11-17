#!/usr/bin/env bash
# =============================================================================
#  Infinity Search — bootstrap for source checkouts (Windows Git Bash / WSL).
#
#  Installs Python and Electron dependencies, creates runtime directories and
#  can fetch the optional heavy engines that are not stored in Git:
#
#    ./scripts/bootstrap.sh                       # Python + npm deps
#    ./scripts/bootstrap.sh --with-playwright     # + Chromium for Playwright
#    ./scripts/bootstrap.sh --with-ffmpeg         # + ffmpeg into app/binaries
#    ./scripts/bootstrap.sh --engine-url URL      # + llama.cpp engine zip
#    ./scripts/bootstrap.sh --hermes-url URL      # + Hermes backend zip
#    ./scripts/bootstrap.sh --all                 # everything above
#
#  A release asset is the recommended way to distribute engine/Hermes:
#  see docs/EXTERNAL.md and scripts/pack-assets.ps1.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_DESKTOP=1
WITH_PLAYWRIGHT=0
WITH_FFMPEG=0
WITH_HERMES=0
ENGINE_URL="${INFINITY_ENGINE_URL:-}"
ENGINE_FROM="${INFINITY_ENGINE_FROM:-}"
HERMES_URL="${INFINITY_HERMES_URL:-}"
HERMES_FROM="${INFINITY_HERMES_FROM:-}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Infinity Search bootstrap

Usage: ./scripts/bootstrap.sh [options]

  --with-playwright       install Chromium for Playwright
  --with-ffmpeg           download ffmpeg/ffprobe into app/binaries/ffmpeg
  --with-hermes           create app/hermes_module/.venv (requires Python 3.11–3.13)
  --engine-url URL        download llama.cpp engine zip into engine/
  --engine-from DIR       copy engine files from a local portable build
  --hermes-url URL        download Hermes backend/frontend zip
  --hermes-from DIR       copy backend/ and frontend/ from a local build
  --no-desktop            skip npm install for desktop/
  --all                   playwright + ffmpeg + hermes venv
  -h, --help              this help

Environment overrides: PYTHON, INFINITY_ENGINE_URL, INFINITY_HERMES_URL,
INFINITY_ENGINE_FROM, INFINITY_HERMES_FROM.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-playwright) WITH_PLAYWRIGHT=1 ;;
    --with-ffmpeg)     WITH_FFMPEG=1 ;;
    --with-hermes)     WITH_HERMES=1 ;;
    --engine-url)      ENGINE_URL="${2:-}"; shift ;;
    --engine-from)     ENGINE_FROM="${2:-}"; shift ;;
    --hermes-url)      HERMES_URL="${2:-}"; shift ;;
    --hermes-from)     HERMES_FROM="${2:-}"; shift ;;
    --no-desktop)      WITH_DESKTOP=0 ;;
    --all)             WITH_PLAYWRIGHT=1; WITH_FFMPEG=1; WITH_HERMES=1 ;;
    -h|--help)         usage; exit 0 ;;
    *)                 die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Python 3.12+
# ---------------------------------------------------------------------------
say "Looking for Python 3.12+"
PY_CMD=()
candidates=()
[[ -n "${PYTHON:-}" ]] && candidates+=("$PYTHON")
candidates+=(python3.13 python3.12 python3 python)

for c in "${candidates[@]}"; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PY_CMD=("$c"); break
  fi
done

if [[ ${#PY_CMD[@]} -eq 0 ]] && command -v py >/dev/null 2>&1; then
  for v in 3.13 3.12; do
    if py "-$v" -c 'import sys; raise SystemExit(0)' 2>/dev/null; then
      PY_CMD=(py "-$v"); break
    fi
  done
fi

[[ ${#PY_CMD[@]} -gt 0 ]] || die "Python 3.12+ not found. Install it from https://python.org and re-run."
"${PY_CMD[@]}" --version

# ---------------------------------------------------------------------------
# Virtualenv + pip dependencies
# ---------------------------------------------------------------------------
if [[ ! -d .venv ]]; then
  say "Creating .venv"
  "${PY_CMD[@]}" -m venv .venv
fi

if [[ -x .venv/Scripts/python.exe ]]; then
  VPY=".venv/Scripts/python.exe"
else
  VPY=".venv/bin/python"
fi
[[ -x "$VPY" ]] || die "venv interpreter not found: $VPY"

say "Installing Python dependencies (this can take a while: torch/transformers are large)"
"$VPY" -m pip install --upgrade pip
"$VPY" -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime directories
# ---------------------------------------------------------------------------
say "Creating runtime directories"
mkdir -p data logs apps models/gguf models/piper models/whisper engine
# The app creates the rest on first run.

# ---------------------------------------------------------------------------
# Electron shell
# ---------------------------------------------------------------------------
if [[ "$WITH_DESKTOP" -eq 1 ]]; then
  if command -v npm >/dev/null 2>&1; then
    say "Installing Electron dependencies (desktop/)"
    (cd desktop && npm install)
    if [[ -f app/torrent_helper/package.json ]]; then
      say "Installing torrent helper dependencies (app/torrent_helper/)"
      (cd app/torrent_helper && npm install)
    fi
  else
    warn "npm not found — desktop/ dependencies skipped (web UI still works)"
  fi
fi

# ---------------------------------------------------------------------------
# Playwright browsers
# ---------------------------------------------------------------------------
if [[ "$WITH_PLAYWRIGHT" -eq 1 ]]; then
  say "Installing Playwright Chromium"
  "$VPY" -m playwright install chromium
fi

# ---------------------------------------------------------------------------
# Helpers for archives
# ---------------------------------------------------------------------------
fetch() { # fetch URL DEST
  local url="$1" dest="$2"
  say "Downloading $url"
  if command -v curl >/dev/null 2>&1; then
    curl -L --retry 3 --fail -o "$dest" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$dest" "$url"
  else
    die "curl or wget is required to download $url"
  fi
}

extract_zip() { # extract_zip ZIP DEST [FLATTEN]
  "$VPY" - "$1" "$2" "${3:-0}" <<'PY'
import os, shutil, sys, tempfile, zipfile

src, dst, flatten = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
os.makedirs(dst, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="infinity-unzip-")
try:
    with zipfile.ZipFile(src) as z:
        # Refuse path traversal.
        for name in z.namelist():
            target = os.path.realpath(os.path.join(tmp, name))
            if not target.startswith(os.path.realpath(tmp) + os.sep):
                raise SystemExit("unsafe path in archive: %s" % name)
        z.extractall(tmp)
    entries = [e for e in os.listdir(tmp) if e not in (".", "..")]
    if flatten and len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
        src_dir = os.path.join(tmp, entries[0])
    else:
        src_dir = tmp
    for name in os.listdir(src_dir):
        s = os.path.join(src_dir, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
}

# ---------------------------------------------------------------------------
# ffmpeg (optional)
# ---------------------------------------------------------------------------
if [[ "$WITH_FFMPEG" -eq 1 ]]; then
  say "Installing ffmpeg into app/binaries/ffmpeg"
  tmp_zip="$(mktemp -t infinity-ffmpeg-XXXXXX.zip)"
  fetch "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "$tmp_zip"
  mkdir -p app/binaries/ffmpeg
  extract_zip "$tmp_zip" app/binaries/ffmpeg 0
  rm -f "$tmp_zip"
  warn "ffmpeg extracted; restart the app so PATH picks it up."
fi

# ---------------------------------------------------------------------------
# llama.cpp engine (optional, custom build required)
# ---------------------------------------------------------------------------
if [[ -n "$ENGINE_FROM" ]]; then
  [[ -d "$ENGINE_FROM" ]] || die "engine source not found: $ENGINE_FROM"
  say "Copying engine from $ENGINE_FROM"
  mkdir -p engine
  cp -a "$ENGINE_FROM"/. engine/
elif [[ -n "$ENGINE_URL" ]]; then
  say "Downloading engine into engine/"
  tmp_zip="$(mktemp -t infinity-engine-XXXXXX.zip)"
  fetch "$ENGINE_URL" "$tmp_zip"
  extract_zip "$tmp_zip" engine 1
  rm -f "$tmp_zip"
else
  warn "engine/ is empty. The app starts, but local AI is disabled until you add"
  warn "llama-server.exe (see docs/EXTERNAL.md). Use --engine-url/--engine-from."
fi

# ---------------------------------------------------------------------------
# Hermes Agent backend/frontend (optional)
# ---------------------------------------------------------------------------
hermes_present=0
if [[ -n "$HERMES_FROM" ]]; then
  [[ -d "$HERMES_FROM" ]] || die "Hermes source not found: $HERMES_FROM"
  say "Copying Hermes backend/frontend from $HERMES_FROM"
  mkdir -p app/hermes_module
  if [[ -d "$HERMES_FROM/backend" ]]; then cp -a "$HERMES_FROM/backend" app/hermes_module/; fi
  if [[ -d "$HERMES_FROM/frontend" ]]; then cp -a "$HERMES_FROM/frontend" app/hermes_module/; fi
  hermes_present=1
elif [[ -n "$HERMES_URL" ]]; then
  say "Downloading Hermes backend/frontend"
  tmp_zip="$(mktemp -t infinity-hermes-XXXXXX.zip)"
  fetch "$HERMES_URL" "$tmp_zip"
  extract_zip "$tmp_zip" app/hermes_module 1
  rm -f "$tmp_zip"
  hermes_present=1
elif [[ -d app/hermes_module/backend && -d app/hermes_module/frontend ]]; then
  hermes_present=1
else
  warn "Hermes backend/frontend are not present. The /hermes section will show"
  warn "diagnostics instead of the agent (see docs/EXTERNAL.md)."
fi

if [[ "$hermes_present" -eq 1 && "$WITH_HERMES" -eq 1 ]]; then
  say "Preparing Hermes virtualenv (Python 3.11–3.13)"
  HPY=()
  for c in python3.13 python3.12 python3.11; do
    if command -v "$c" >/dev/null 2>&1; then HPY=("$c"); break; fi
  done
  if [[ ${#HPY[@]} -eq 0 ]] && command -v py >/dev/null 2>&1; then
    for v in 3.13 3.12 3.11; do
      if py "-$v" -c 'import sys; raise SystemExit(0)' 2>/dev/null; then HPY=(py "-$v"); break; fi
    done
  fi
  if [[ ${#HPY[@]} -gt 0 ]]; then
    "${HPY[@]}" -m venv app/hermes_module/.venv
    if [[ -x app/hermes_module/.venv/Scripts/python.exe ]]; then HPV="app/hermes_module/.venv/Scripts/python.exe"; else HPV="app/hermes_module/.venv/bin/python"; fi
    "$HPV" -m pip install --upgrade pip
    "$HPV" -m pip install -r app/hermes_module/requirements-module.txt
  else
    warn "Python 3.11–3.13 not found. Install it and run:"
    warn "  python3.13 -m venv app/hermes_module/.venv"
    warn "  app/hermes_module/.venv/Scripts/python.exe -m pip install -r app/hermes_module/requirements-module.txt"
  fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
say "Bootstrap complete"
cat <<EOF

Next steps:
  1. Windows:   run-dev.bat
     Git Bash:  .venv/Scripts/python.exe app/main.py --server --port 8281
  2. Open http://127.0.0.1:8281

Optional components and exact paths: docs/EXTERNAL.md
EOF
