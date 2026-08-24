#!/usr/bin/env bash
set -euo pipefail

# G2-only lane for the arXiv sample plugin's front-end half
# (examples/extensions/arxiv-search/ui/arxiv-search/). It is not part of
# scripts/check.sh (G1) because it is the one place in the repository that
# deliberately synchronizes a UI package into frontend/features/ext-*/ —
# `npm run test` (G1) asserts that tree stays empty by default
# (CLAUDE.md "Workspace UI registry": extension-ui-host.component.test.tsx's
# "length 1 with zero plugins" must not be relaxed), so exercising the
# sample package needs its own, separately-triggered pass.
#
# It runs, in order:
#   1. `sync-ui-plugins.mjs` with SILICON_NOTEBOOK_UI_PLUGINS pointed at the
#      sample package — the exact tool + env var a deployment would use
#      (docs/deployment-extensions-sop.md), not a hand-rolled copy.
#   2. The five `frontend/tests/guards/extension-*.test.mjs` suites, now
#      exercising a *non-empty* `features/ext-*/` for the first time in this
#      repository (AGENTS.md §0.4 / R8): those guards' "features/ext-*/ is
#      always empty in the public repo" branches finally get a real sample.
#   3. `npm run lint` (tsc --noEmit) — `prelint` re-syncs with this same
#      env var (idempotent), and this is the check that actually type-checks
#      the copied package (next build's type-check silently drops
#      *.test.* files, but nothing here is a test file).
#
# The trap is load-bearing, not tidiness (R5): if this script is interrupted
# mid-run, frontend/features/ext-arxiv-search/ would otherwise stay on disk
# and every *subsequent* `npm run test` (G1) would fail — the copied package
# is exactly what extension-ui-host.component.test.tsx's "length 1" asserts
# must not exist by default. Manual recovery, if a run is killed hard enough
# to skip even the trap, is the same one-liner:
#   cd frontend && SILICON_NOTEBOOK_UI_PLUGINS= node scripts/sync-ui-plugins.mjs

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT_DIR/examples/extensions/arxiv-search/ui/arxiv-search"

cleanup() {
  (cd "$ROOT_DIR/frontend" && SILICON_NOTEBOOK_UI_PLUGINS= node scripts/sync-ui-plugins.mjs) || true
}
trap cleanup EXIT

cd "$ROOT_DIR/frontend"
export SILICON_NOTEBOOK_UI_PLUGINS="$PKG"
node scripts/sync-ui-plugins.mjs
node --test tests/guards/extension-*.test.mjs
npm run lint
