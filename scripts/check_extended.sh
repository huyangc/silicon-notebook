#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Level 2 is intentionally exhaustive and is not subject to the Level 1
# edit-time Apple Silicon <=60s target. CI schedules this wrapper once daily.
bash "$ROOT_DIR/scripts/check.sh"
bash "$ROOT_DIR/scripts/check_backend_extended.sh"

# B5 facade-retirement ratchet: fail the day's run if any RepositoryFacade
# public method has drifted into zero-callers (retire-now) territory —
# see docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md. Read-only,
# no repository construction; cheap enough to belong here but slow enough
# (~6s, one AST pass over backend/app + scripts + backend/tests) that G1
# does not pay for it on every edit.
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/audit_facade_callers.py" --assert-no-retire-now
