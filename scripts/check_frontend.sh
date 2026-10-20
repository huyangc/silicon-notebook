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

# One supported Node.js major (.nvmrc); see scripts/node_version.sh.
source "$ROOT_DIR/scripts/node_version.sh"
require_node_major "$(node -v 2>/dev/null || true)" "the frontend lane" || exit 1

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
# frontend/tests/** never fails the build. `npm run typecheck` (tsc --noEmit) is the
# one pass that sees those files. It runs after the build so `.next/types` has just
# been regenerated (a stale tree cannot fail on generated route types), and
# `incremental` keeps the warm re-check under a second (~5s cold).
build_then_typecheck() {
  run_step build env npm_config_ignore_scripts=true npm run build \
    && run_step typecheck env npm_config_ignore_scripts=true npm run typecheck
}

# `npm run lint` is ESLint against the eslint-suppressions.json baseline: a
# violation the baseline does not cover fails it, and so does a baseline entry
# that no longer occurs (exit 2), so the baseline only ever shrinks. It reads
# source files only (no build output, no plugin projection), so it is its own
# group; `next build` does not lint (`eslint.ignoreDuringBuilds`).
#
# On a host with at least 8 CPUs the four independent groups -- node tests,
# component tests, ESLint, build followed by typecheck -- run concurrently, each
# into its own log that is printed in a fixed order once all of them finish.
# Smaller hosts (CI runners) keep the sequential order and stop at the first
# failure.
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
  run_step lint npm run lint
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
run_step lint npm run lint &
GROUP_PIDS+=("$!")
build_then_typecheck &
GROUP_PIDS+=("$!")

status=0
for pid in "${GROUP_PIDS[@]}"; do
  wait "$pid" || status=1
done

# A step without an exit file either never started (only the typecheck, and
# only after a failed build) or was killed before run_step could record one.
declare -a FAILED_STEPS=()
for step in node-tests component-tests lint build typecheck; do
  if [[ ! -f "$STEP_LOG_DIR/$step.exit" ]]; then
    if [[ "$step" == typecheck && -f "$STEP_LOG_DIR/build.exit" \
          && "$(cat "$STEP_LOG_DIR/build.exit")" != 0 ]]; then
      printf '\n----- frontend %s: not run (build failed) -----\n' "$step"
      continue
    fi
    printf '\n----- frontend %s: no exit status recorded (interrupted) -----\n' "$step"
    if [[ -f "$STEP_LOG_DIR/$step.log" ]]; then
      cat "$STEP_LOG_DIR/$step.log"
    fi
    FAILED_STEPS+=("$step (interrupted)")
    status=1
    continue
  fi
  printf '\n----- frontend %s: exit=%s %ss -----\n' "$step" \
    "$(cat "$STEP_LOG_DIR/$step.exit")" "$(cat "$STEP_LOG_DIR/$step.seconds")"
  cat "$STEP_LOG_DIR/$step.log"
  if [[ "$(cat "$STEP_LOG_DIR/$step.exit")" != 0 ]]; then
    FAILED_STEPS+=("$step (exit $(cat "$STEP_LOG_DIR/$step.exit"))")
  fi
done
for failed in "${FAILED_STEPS[@]:-}"; do
  if [[ -n "$failed" ]]; then
    printf 'frontend step failed: %s\n' "$failed" >&2
  fi
done
exit "$status"
