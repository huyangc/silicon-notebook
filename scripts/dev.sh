#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PYTHON="/opt/homebrew/Caskroom/miniconda/base/bin/python"
PYTHON_BIN="${PYTHON_BIN:-$DEFAULT_PYTHON}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]]; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT

# Load the repo-root .env so BOTH processes see the same vars. The backend reads
# it via pydantic regardless, but the Next.js frontend only reads frontend/.env*
# — without this, NEXT_PUBLIC_API_BASE_URL set in the root .env never reaches it.
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090,SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

# BACKEND_HOST=0.0.0.0 to expose the API beyond localhost (e.g. server deploys).
cd "$ROOT_DIR/backend"
"$PYTHON_BIN" -m uvicorn app.main:app --host "${BACKEND_HOST:-127.0.0.1}" --port 8000 &
BACKEND_PID=$!

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi
cd "$ROOT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

wait "$BACKEND_PID" "$FRONTEND_PID"
