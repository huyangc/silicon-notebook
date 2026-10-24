#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Level 2 is intentionally exhaustive and is not subject to the Level 1
# edit-time Apple Silicon <=100s target. Locally, run it with no arguments: it
# is G1 (check.sh) followed by everything G2 adds on top.
#
# --complement-only skips the G1 step and runs only what G2 adds. It exists for
# .github/workflows/daily-extended.yml alone, which runs G1's lanes as sibling
# jobs on their own runners (the same backend split and sequential placement
# of the smaller part before contracts/frontend as PR CI) and
# fails the day's run unless every one of them and this job succeed. One 4-vCPU
# runner cannot fit G1 and the complement inside a job timeout: from
# 2026-10-07 every single-job run was cancelled before reaching the end.
# A local run with the flag is not a G2 run.
RUN_STANDARD_GATE=1
while (( $# )); do
  case "$1" in
    --complement-only) RUN_STANDARD_GATE=0; shift ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

if (( RUN_STANDARD_GATE )); then
  bash "$ROOT_DIR/scripts/check.sh"
fi
bash "$ROOT_DIR/scripts/check_backend_extended.sh"

# arXiv sample plugin's front-end half: the one lane that synchronizes a UI
# package into frontend/features/ext-*/ and exercises the five
# extension-*.test.mjs guards against a non-empty sample (X9 PR-B T3,
# docs/development*.md 的扩展门约定). Deliberately not part of G1 — see
# scripts/check_sample_plugin.sh's own header comment for why.
bash "$ROOT_DIR/scripts/check_sample_plugin.sh"

# B5 facade-retirement ratchet: fail the day's run if any RepositoryFacade
# public method has drifted into zero-callers (retire-now) territory —
# see docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md. Read-only,
# no repository construction; cheap enough to belong here but slow enough
# (~6s, one AST pass over backend/app + scripts + backend/tests) that G1
# does not pay for it on every edit.
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/audit_facade_callers.py" --assert-no-retire-now
