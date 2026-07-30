#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Level 2 is intentionally exhaustive and is not subject to the Level 1
# edit-time Apple Silicon <=60s target. CI schedules this wrapper once daily.
bash "$ROOT_DIR/scripts/check.sh"
bash "$ROOT_DIR/scripts/check_backend_extended.sh"
