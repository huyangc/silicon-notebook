#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CHECK_LANE_NAME="${CHECK_LANE_NAME:-frontend}"
CHECK_TIMING_FILE="${CHECK_TIMING_FILE:-/dev/stdout}"
START_SECONDS=$SECONDS
STEP_LOG_DIR=""
finish() {
  if [[ -n "$STEP_LOG_DIR" ]]; then
    rm -rf "$STEP_LOG_DIR"
  fi
  printf "%s=%s\n" "$CHECK_LANE_NAME" "$((SECONDS - START_SECONDS))" > "$CHECK_TIMING_FILE"
}
trap finish EXIT

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "frontend/node_modules not found; run 'npm install' in frontend/ first" >&2
  exit 1
fi

cd "$ROOT_DIR/frontend"
# Every standalone lifecycle keeps its own pre-hook so direct developer commands
# remain self-contained.  The complete lane has one immutable plugin projection,
# so sync it once and suppress only the redundant npm pre-hooks below.
npm run sync:ui-plugins

# `npm run test` is exactly `test:node && test:component` (pinned by
# backend/tests/test_test_architecture_policy.py); the lane runs the two halves
# as separate steps so they can overlap with the build.
#
# `next build` keeps TypeScript errors fatal for production code (`ignoreBuildErrors`
# stays unset), but Next's build-time type checker silently drops every diagnostic in
# `*.test.*`/`*.spec.*` files and `__tests__`/`__mocks__` directories (the ignoreRegex
# in next/dist/lib/typescript/runTypeCheck.js), so a type error that only exists under
# frontend/tests/** never fails the build. `npm run lint` (tsc --noEmit) is the one
# pass that sees those files. It runs after the build so `.next/types` has just been
# regenerated (a stale tree cannot fail on generated route types), and `incremental`
# keeps the warm re-check under a second (~5s cold).
build_then_typecheck() {
  run_step build env npm_config_ignore_scripts=true npm run build \
    && run_step typecheck env npm_config_ignore_scripts=true npm run lint
}

# On a host with at least 8 CPUs the three independent groups -- node tests,
# component tests, build followed by typecheck -- run concurrently, each into its
# own log that is printed in a fixed order once all of them finish. Smaller hosts
# (CI runners) keep the sequential order and stop at the first failure.
# FRONTEND_LANE_PARALLEL=1/0 overrides the CPU check. Every step stays in this
# lane's process group, so check.sh's group kill still reaps all of them.
if [[ -n "${FRONTEND_LANE_PARALLEL:-}" ]]; then
  parallel="$FRONTEND_LANE_PARALLEL"
elif (( $(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1) >= 8 )); then
  parallel=1
else
  parallel=0
fi

if [[ "$parallel" != 1 ]]; then
  run_step() { shift; "$@"; }
  run_step node-tests npm run test:node
  run_step component-tests npm run test:component
  build_then_typecheck
  exit 0
fi

STEP_LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/silicon-frontend.XXXXXX")"
run_step() {
  local step="$1" started=$SECONDS status=0
  shift
  "$@" >"$STEP_LOG_DIR/$step.log" 2>&1 || status=$?
  printf '%s\n' "$status" >"$STEP_LOG_DIR/$step.exit"
  printf '%s\n' "$((SECONDS - started))" >"$STEP_LOG_DIR/$step.seconds"
  return "$status"
}

declare -a GROUP_PIDS=()
run_step node-tests npm run test:node &
GROUP_PIDS+=("$!")
run_step component-tests npm run test:component &
GROUP_PIDS+=("$!")
build_then_typecheck &
GROUP_PIDS+=("$!")

status=0
for pid in "${GROUP_PIDS[@]}"; do
  wait "$pid" || status=1
done

for step in node-tests component-tests build typecheck; do
  if [[ ! -f "$STEP_LOG_DIR/$step.exit" ]]; then
    printf '\n----- frontend %s: not run (build failed) -----\n' "$step"
    continue
  fi
  printf '\n----- frontend %s: exit=%s %ss -----\n' "$step" \
    "$(cat "$STEP_LOG_DIR/$step.exit")" "$(cat "$STEP_LOG_DIR/$step.seconds")"
  cat "$STEP_LOG_DIR/$step.log"
done
for step in node-tests component-tests build typecheck; do
  if [[ -f "$STEP_LOG_DIR/$step.exit" && "$(cat "$STEP_LOG_DIR/$step.exit")" != 0 ]]; then
    printf 'frontend step failed: %s (exit %s)\n' "$step" "$(cat "$STEP_LOG_DIR/$step.exit")" >&2
  fi
done
exit "$status"
