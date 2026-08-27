#!/usr/bin/env bash
set -euo pipefail

# G2-only lane for the arXiv sample plugin's front-end half
# (examples/extensions/arxiv-search/ui/arxiv-search/). It is not part of
# scripts/check.sh (G1) because it is the one place in the repository that
# deliberately synchronizes a UI package into frontend/features/ext-*/ —
# `npm run test` (G1) asserts that tree stays empty by default
# (`docs/development.md` workspace UI registry: extension-ui-host.component.test.tsx's
# "length 1 with zero plugins" must not be relaxed), so exercising the
# sample package needs its own, separately-triggered pass.
#
# It runs, in order:
#   1. `sync-ui-plugins.mjs` with SILICON_NOTEBOOK_UI_PLUGINS pointed at the
#      sample package — the exact tool + env var a deployment would use
#      (docs/deployment-extensions-sop.md), not a hand-rolled copy.
#   2. A sanity check that the copy actually landed. Without it every step
#      below would still pass on an *empty* features/ext-arxiv-search/ — the
#      guards' "the tree is empty" branches are green either way, and tsc has
#      nothing to complain about — so a silently broken sync would read as a
#      clean lane. This is the one assertion that makes the rest non-vacuous.
#   3. `npm run test:node` — the whole node lane (tests/unit + tests/guards),
#      not just `extension-*.test.mjs`. Measured at zero practical cost (2441
#      tests green) and it buys the rest of the suite a run against a
#      *non-empty* `features/ext-*/`, which is the configuration no other
#      lane ever sees (see docs/development*.md). `test:node` is deliberately the
#      script name rather than `test`: npm fires `pre<script>` for the exact
#      name, so this does not re-trigger `pretest`'s sync underneath us.
#   4. `check_ui_vocabulary.py` — the synced copy lands inside
#      `frontend/features`, which is already in that guard's rglob scan face,
#      so this brings the sample's Chinese UI strings under the vocabulary
#      rail without needing an `--extra-root` (the plugin's *Python* half does
#      need one, and gets it in check_contracts.sh).
#   5. `npm run lint` (tsc --noEmit) — `prelint` re-syncs with this same
#      env var (idempotent), and this is the check that actually type-checks
#      the copied package (next build's type-check silently drops
#      *.test.* files, but nothing here is a test file).
#
# The trap is load-bearing, not tidiness (R5): if this script is interrupted
# mid-run, frontend/features/ext-arxiv-search/ would otherwise stay on disk
# and every *subsequent* `npm run test` (G1) would fail — the copied package
# is exactly what extension-ui-host.component.test.tsx's "length 1" asserts
# must not exist by default.
#
# Cleanup **restores**, it does not clear: a deployment box (or a developer
# working on a private plugin) may well have SILICON_NOTEBOOK_UI_PLUGINS set
# already, and unconditionally re-syncing with an empty value would delete
# their working tree's plugins and leave them wondering why the app lost a
# panel. So the original value is captured up front and re-applied on exit.
#
# ⚠ Concurrency: this lane mutates a tree that `scripts/check.sh` (G1) reads,
# so running the two at the same time can make either one fail spuriously.
# Serialization is guaranteed where it matters — check_extended.sh invokes
# this lane sequentially — but an editor-triggered `check.sh` racing a manual
# run of this script is on you.
#
# Manual recovery, if a run is killed hard enough to skip even the trap:
#   cd frontend && SILICON_NOTEBOOK_UI_PLUGINS="<your original value or empty>" \
#     node scripts/sync-ui-plugins.mjs

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PKG="$ROOT_DIR/examples/extensions/arxiv-search/ui/arxiv-search"
SYNCED="$ROOT_DIR/frontend/features/ext-arxiv-search"
# Captured before the export below, so it is the caller's value, not ours.
ORIG_UI_PLUGINS="${SILICON_NOTEBOOK_UI_PLUGINS:-}"

cleanup() {
  # `|| true` keeps the trap from replacing the real exit status, but a failed
  # restore must not be silent: it leaves the sample package on disk and the
  # next G1 run will fail somewhere that looks unrelated to this script.
  if ! (
    cd "$ROOT_DIR/frontend" \
      && SILICON_NOTEBOOK_UI_PLUGINS="$ORIG_UI_PLUGINS" node scripts/sync-ui-plugins.mjs
  ); then
    echo "check_sample_plugin: WARNING — failed to restore the UI plugin tree." >&2
    echo "check_sample_plugin: run this before your next check.sh:" >&2
    echo "  (cd '$ROOT_DIR/frontend' && SILICON_NOTEBOOK_UI_PLUGINS='$ORIG_UI_PLUGINS' node scripts/sync-ui-plugins.mjs)" >&2
  fi
}
trap cleanup EXIT

cd "$ROOT_DIR/frontend"
export SILICON_NOTEBOOK_UI_PLUGINS="$PKG"
node scripts/sync-ui-plugins.mjs
test -f "$SYNCED/workspace-plugin.tsx" \
  || { echo "check_sample_plugin: sync produced no $SYNCED/workspace-plugin.tsx" >&2; exit 1; }
npm run test:node
PYTHONPATH="$ROOT_DIR/backend" "$PYTHON_BIN" "$ROOT_DIR/scripts/check_ui_vocabulary.py"
npm run lint
