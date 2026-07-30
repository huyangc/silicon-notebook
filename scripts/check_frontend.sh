#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CHECK_LANE_NAME="${CHECK_LANE_NAME:-frontend}"
CHECK_TIMING_FILE="${CHECK_TIMING_FILE:-/dev/stdout}"
START_SECONDS=$SECONDS
trap 'printf "%s=%s\n" "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))" > "$CHECK_TIMING_FILE"' EXIT

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi

cd "$ROOT_DIR/frontend"
npm run test
# `next build` owns the G1 typecheck and fails closed on TypeScript errors.
# Keep the package typecheck as a targeted G0 command; running the same TypeScript pass
# immediately before Next's type-validation pass only parses the program twice.
npm run build
