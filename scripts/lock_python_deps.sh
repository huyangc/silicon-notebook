#!/usr/bin/env bash
# scripts/lock_python_deps.sh — regenerate the hash-locked Python dependency files.
#
#   backend/requirements.in      runtime ranges   -> backend/requirements.txt
#   backend/requirements-dev.in  test/gate tools  -> backend/requirements-dev.txt
#                                (constrained by the runtime lock: shared
#                                packages keep the runtime lock's versions)
#
# Usage:
#   bash scripts/lock_python_deps.sh
#       Relock after editing a .in file. Every existing pin that still satisfies
#       the ranges is kept; only what the edit requires changes.
#   bash scripts/lock_python_deps.sh --upgrade-package NAME [--upgrade-package NAME ...]
#       Upgrade only the named packages (NAME or NAME==VERSION) to the newest
#       versions the ranges allow.
#   bash scripts/lock_python_deps.sh --upgrade
#       Upgrade every package to the newest version the ranges allow.
#   bash scripts/lock_python_deps.sh --check
#       Relock into a temporary copy and fail if the committed lock files would
#       change. Needs the package index; the offline gate is
#       backend/tests/test_python_lockfiles.py.
#
# Upgrading is always one of the explicit commands above: installing never
# resolves anything (pip install --require-hashes -r <lock>).
#
# The lock is resolved for CPython 3.13, the version CI and production run.
# LOCK_PYTHON must be a 3.13 interpreter (default: python3.13). pip-tools runs
# from a private venv under .local/, pinned to PIP_TOOLS_VERSION/PIP_VERSION
# so two machines produce the same files. The hashes cover every file PyPI
# publishes for each pinned version, so one lock installs on macOS and Linux;
# resolving on either platform gives identical files as long as no dependency
# is platform-conditional between them (if one becomes so, a lock made on the
# other platform misses it and `pip install --require-hashes` fails loudly).
#
# Environment:
#   LOCK_PYTHON        CPython 3.13 interpreter (default python3.13)
#   PIP_INDEX_URL      package index to resolve against (never written into the lock)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_PYTHON="${LOCK_PYTHON:-python3.13}"
PIP_TOOLS_VERSION="7.6.2"
PIP_VERSION="26.2"
TOOLS_VENV="$ROOT_DIR/.local/pip-tools-py313"
COMPILE_COMMAND="bash scripts/lock_python_deps.sh"

die() { printf '[lock_python_deps:error] %s\n' "$*" >&2; exit 2; }
log() { printf '[lock_python_deps] %s\n' "$*"; }

mode="lock"
upgrade_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) mode="check"; shift ;;
    --upgrade) upgrade_args+=(--upgrade); shift ;;
    --upgrade-package)
      [[ $# -ge 2 && -n "$2" ]] || die "--upgrade-package needs a package name"
      upgrade_args+=(--upgrade-package "$2"); shift 2 ;;
    -h|--help) sed -n '2,37p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
if [[ "$mode" == "check" && ${#upgrade_args[@]} -gt 0 ]]; then
  die "--check cannot be combined with an upgrade"
fi

command -v "$LOCK_PYTHON" >/dev/null 2>&1 \
  || die "$LOCK_PYTHON not found; set LOCK_PYTHON to a CPython 3.13 interpreter."
"$LOCK_PYTHON" - <<'PY' || die "LOCK_PYTHON must be CPython 3.13 (the version CI and production run)."
import platform, sys
raise SystemExit(0 if sys.version_info[:2] == (3, 13) and platform.python_implementation() == "CPython" else 1)
PY

tools_ready() {
  [[ -x "$TOOLS_VENV/bin/python" ]] || return 1
  "$TOOLS_VENV/bin/python" - "$PIP_TOOLS_VERSION" "$PIP_VERSION" <<'PY'
import sys
from importlib.metadata import PackageNotFoundError, version
try:
    ok = (sys.version_info[:2] == (3, 13)
          and version("pip-tools") == sys.argv[1]
          and version("pip") == sys.argv[2])
except PackageNotFoundError:
    ok = False
raise SystemExit(0 if ok else 1)
PY
}

if ! tools_ready; then
  log "preparing pip-tools $PIP_TOOLS_VERSION in $TOOLS_VENV"
  rm -rf "$TOOLS_VENV"
  mkdir -p "$(dirname "$TOOLS_VENV")"
  "$LOCK_PYTHON" -m venv "$TOOLS_VENV"
  "$TOOLS_VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
    "pip==$PIP_VERSION" "pip-tools==$PIP_TOOLS_VERSION"
  tools_ready || die "pip-tools venv at $TOOLS_VENV is not usable"
fi

compile_lock() {
  # $1 = directory holding the .in/.txt pair, $2 = input name, $3 = output name
  (
    cd "$1"
    CUSTOM_COMPILE_COMMAND="$COMPILE_COMMAND" \
    "$TOOLS_VENV/bin/python" -m piptools compile \
      --quiet \
      --resolver=backtracking \
      --generate-hashes \
      --allow-unsafe \
      --strip-extras \
      --no-emit-index-url \
      --no-emit-trusted-host \
      --newline=lf \
      ${upgrade_args[@]+"${upgrade_args[@]}"} \
      --output-file "$3" \
      "$2"
    # The offline gate (backend/tests/test_python_lockfiles.py) compares this
    # stamp with the .in file, so an edit that was never relocked fails it.
    "$TOOLS_VENV/bin/python" "$ROOT_DIR/scripts/python_lockfiles.py" stamp "$2" "$3"
  )
}

lock_into() {
  # Runtime first: the dev lock is constrained by it.
  compile_lock "$1" requirements.in requirements.txt
  compile_lock "$1" requirements-dev.in requirements-dev.txt
}

LOCK_FILES=(requirements.in requirements-dev.in requirements.txt requirements-dev.txt)

if [[ "$mode" == "lock" ]]; then
  lock_into "$ROOT_DIR/backend"
  log "updated backend/requirements.txt and backend/requirements-dev.txt"
  exit 0
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/lock-python-deps.XXXXXX")"
trap 'rm -rf "$work"' EXIT
for name in "${LOCK_FILES[@]}"; do
  cp "$ROOT_DIR/backend/$name" "$work/$name"
done
lock_into "$work"
stale=0
for name in requirements.txt requirements-dev.txt; do
  if ! diff -u "$ROOT_DIR/backend/$name" "$work/$name"; then
    stale=1
  fi
done
if [[ "$stale" -ne 0 ]]; then
  printf '[lock_python_deps:check] backend lock files are stale; run: %s\n' "$COMPILE_COMMAND" >&2
  exit 1
fi
log "lock files are current"
