#!/usr/bin/env bash
# Hand one fixed-spec implementation task to the local Codex CLI.
#
#   codex-impl.sh run    <worktree> <spec.md> [label]   # new codex session
#   codex-impl.sh resume <run-dir>  <followup.md>       # same session, more work
#   codex-impl.sh show   <run-dir>                      # status + final report
#
# Claude writes the spec, runs this (in the background), then reviews the diff
# itself. Codex edits the worktree only: no commit, push, branch or npm install
# (the preamble says so and the script checks HEAD did not move).
#
# Every run gets a directory under $CODEX_IMPL_HOME (default
# ~/.claude/codex-impl/) holding: prompt.md, events.jsonl, stderr.log, last.md
# (codex's final message), session_id, worktree, base_sha, exit_code.
#
# Exit codes: 0 codex finished and left a final report; 1 codex failed
# (non-zero exit, error event, empty report, or the known "CLI too old"
# signatures); 2 usage error; 3 codex moved HEAD (it committed).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${CODEX_IMPL_HOME:-$HOME/.claude/codex-impl}"
# Shared machine-wide lock for heavy jobs (check.sh, PG lane, builds...).
HEAVY_GATE="${HEAVY_GATE:-$HOME/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh}"
HEAVY_LOCK_DIR="${HEAVY_GATE_LOCK_DIR:-$HOME/.cache}"

die() { echo "codex-impl: $*" >&2; exit 2; }

# Codex settings for unattended implementation, passed as -c overrides so the
# user's interactive ~/.codex/config.toml stays untouched. `exec resume` has no
# -C/-s/--add-dir flags, so everything goes through -c and the cwd.
# The worktree's git dir lives in the main checkout and is deliberately NOT a
# writable root, so codex cannot commit. ~/.cache is writable so heavy-gate's
# lock (and tool caches) work. notify=[] silences the desktop turn-ended hook.
codex_overrides() {
  printf '%s\n' \
    -c 'approval_policy="never"' \
    -c 'sandbox_mode="workspace-write"' \
    -c 'sandbox_workspace_write.network_access=true' \
    -c "sandbox_workspace_write.writable_roots=[\"$HEAVY_LOCK_DIR\"]" \
    -c 'notify=[]'
}

check_result() {
  # $1 run dir, $2 codex exit code
  local d="$1" rc="$2" fail=0
  echo "$rc" > "$d/exit_code"
  local sid
  sid="$(grep -m1 '"type":"thread.started"' "$d/events.jsonl" 2>/dev/null \
        | sed -E 's/.*"thread_id":"([^"]+)".*/\1/')"
  [ -n "$sid" ] && [ ! -s "$d/session_id" ] && echo "$sid" > "$d/session_id"

  if [ "$rc" != 0 ]; then echo "codex-impl: codex exited $rc" >&2; fail=1; fi
  if grep -qE '"type":"(error|turn\.failed)"' "$d/events.jsonl" 2>/dev/null; then
    echo "codex-impl: error event in events.jsonl:" >&2
    grep -E '"type":"(error|turn\.failed)"' "$d/events.jsonl" | tail -3 | cut -c1-400 >&2
    fail=1
  fi
  # Known silent-failure signatures: configured model newer than the CLI.
  if grep -qiE 'requires a newer version|Model metadata .* not found|not supported when using Codex' \
       "$d/stderr.log" "$d/events.jsonl" 2>/dev/null; then
    echo "codex-impl: codex CLI looks older than the configured model; upgrade it (npm i -g @openai/codex@latest or brew upgrade codex) and resume." >&2
    fail=1
  fi
  if [ ! -s "$d/last.md" ]; then echo "codex-impl: empty final report (last.md)" >&2; fail=1; fi

  local wt base now
  wt="$(cat "$d/worktree")"; base="$(cat "$d/base_sha")"
  now="$(git -C "$wt" rev-parse HEAD)"
  if [ "$now" != "$base" ]; then
    echo "codex-impl: HEAD moved $base -> $now (codex must not commit)" >&2
    return 3
  fi
  return "$fail"
}

run_codex() {
  # $1 run dir, $2 worktree, rest: codex args
  local d="$1" wt="$2"; shift 2
  local overrides=() line
  while IFS= read -r line; do overrides+=("$line"); done < <(codex_overrides)
  ( cd "$wt" && codex exec "${overrides[@]}" --json -o "$d/last.md" "$@" - \
      < "$d/prompt.md" > "$d/events.jsonl" 2> "$d/stderr.log" )
}

preamble() {
  local wt="$1"
  sed -e "s#@WORKTREE@#$wt#g" -e "s#@HEAVY_GATE@#$HEAVY_GATE#g" "$HERE/preamble.md"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  run)
    wt="${1:-}"; spec="${2:-}"; label="${3:-task}"
    [ -n "$wt" ] && [ -n "$spec" ] || die "usage: run <worktree> <spec.md> [label]"
    [ -f "$spec" ] || die "spec not found: $spec"
    wt="$(cd "$wt" 2>/dev/null && pwd)" || die "worktree not found"
    git -C "$wt" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git work tree: $wt"
    command -v codex >/dev/null || die "codex not on PATH"
    [ -f "$HEAVY_GATE" ] || echo "codex-impl: warning: heavy-gate not found at $HEAVY_GATE" >&2

    d="$HOME_DIR/$(date '+%Y%m%d-%H%M%S')-$(printf '%s' "$label" | tr -c 'A-Za-z0-9._-' '-')"
    mkdir -p "$d"
    echo "$wt" > "$d/worktree"
    git -C "$wt" rev-parse HEAD > "$d/base_sha"
    git -C "$wt" status --porcelain > "$d/status_before"
    if [ -s "$d/status_before" ]; then
      echo "codex-impl: warning: worktree is dirty before the run; commit a WIP first so the codex diff is separable (see $d/status_before)" >&2
    fi
    { preamble "$wt"; echo; echo "# 任务规格"; echo; cat "$spec"; } > "$d/prompt.md"
    cp "$spec" "$d/spec.md"
    echo "codex-impl: run dir $d" >&2
    run_codex "$d" "$wt"
    check_result "$d" "$?"; rc=$?
    echo "RUN_DIR=$d"
    echo "EXIT=$rc"
    exit "$rc"
    ;;
  resume)
    d="${1:-}"; follow="${2:-}"
    [ -d "$d" ] && [ -f "$follow" ] || die "usage: resume <run-dir> <followup.md>"
    sid="$(cat "$d/session_id" 2>/dev/null)"; [ -n "$sid" ] || die "no session_id in $d"
    wt="$(cat "$d/worktree")"
    n=$(( $(ls "$d"/followup-*.md 2>/dev/null | wc -l) + 1 ))
    cp "$follow" "$d/followup-$n.md"
    for f in events.jsonl stderr.log last.md; do
      [ -f "$d/$f" ] && mv "$d/$f" "$d/round$((n-1))-$f"
    done
    cp "$follow" "$d/prompt.md"
    git -C "$wt" rev-parse HEAD > "$d/base_sha"
    run_codex "$d" "$wt" resume "$sid"
    check_result "$d" "$?"; rc=$?
    echo "RUN_DIR=$d"
    echo "EXIT=$rc"
    exit "$rc"
    ;;
  show)
    d="${1:-}"; [ -d "$d" ] || die "usage: show <run-dir>"
    echo "worktree:   $(cat "$d/worktree")"
    echo "session_id: $(cat "$d/session_id" 2>/dev/null || echo '?')"
    echo "exit_code:  $(cat "$d/exit_code" 2>/dev/null || echo 'running?')"
    echo "--- final report (last.md) ---"
    cat "$d/last.md" 2>/dev/null || echo "(none yet)"
    ;;
  *)
    die "usage: run <worktree> <spec.md> [label] | resume <run-dir> <followup.md> | show <run-dir>"
    ;;
esac
