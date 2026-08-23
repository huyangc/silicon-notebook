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
# `next build` keeps TypeScript errors fatal for production code (`ignoreBuildErrors`
# stays unset), but Next's build-time type checker silently drops every diagnostic in
# `*.test.*`/`*.spec.*` files and `__tests__`/`__mocks__` directories (the ignoreRegex
# in next/dist/lib/typescript/runTypeCheck.js), so a type error that only exists under
# frontend/tests/** never fails the build. `npm run lint` (tsc --noEmit) is the one
# pass that sees those files. It runs after the build so `.next/types` has just been
# regenerated (a stale tree cannot fail on generated route types), and `incremental`
# keeps the warm re-check under a second (~5s cold).
npm run build
npm run lint
