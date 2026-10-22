#!/usr/bin/env bash
# Sourced by scripts/dev.sh and scripts/prod.sh, after scripts/extension_services.sh
# (it uses extension_services_last_async and extension_services_cleanup, and changes
# nothing there). The shared wind-down of a launcher's own application children.
#
# The launcher keeps its children in BACKEND_PID and FRONTEND_PID (either may be
# unset) and installs launcher_install_traps once. Environment:
#   START_CLEANUP_GRACE_SECONDS  (default 10) from SIGTERM to SIGKILL
#   _SCRIPT_TEST_CLEANUP_MAX_POLLS / _SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS
#                                test hooks of the grace wait
#
# Exit sequence, on every exit path and on Ctrl-C / TERM / HUP (130 / 143 / 129):
# SIGTERM to the children together, a grace window, SIGKILL for whatever is still
# running, reap, and only then the companion services this launch started.
# During the wait for the children (up to the SIGKILL and the reaping after it),
# INT / TERM / HUP no longer end the launcher: a further one escalates (stop
# waiting, SIGKILL the survivors now) and the companion cleanup still runs after
# the reaping. The exit status stays that of the first cause. The exception is the
# companion stop itself: it runs in the foreground, so a Ctrl-C that reaches it
# interrupts that command, as it always did.

# START_CLEANUP_GRACE_SECONDS must be a positive integer; refuse before anything starts.
launcher_check_grace_seconds() {
  START_CLEANUP_GRACE_SECONDS="${START_CLEANUP_GRACE_SECONDS:-10}"
  if [[ ! "$START_CLEANUP_GRACE_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "错误: START_CLEANUP_GRACE_SECONDS 必须是正整数。" >&2
    exit 2
  fi
}

# A child is forked as `( exec ... ) &` and its pid read from `$!` on the next
# line. A cancellation pending when the fork returns runs its trap between the two
# (macOS /bin/bash 3.2 often does this for a signal that lands next to the fork),
# when the pid is in no variable yet and cleanup would leave the child running.
# Bracket every such fork, naming the variable that will receive the pid:
#   launcher_fork_mark FRONTEND_PID
#   ( exec ... ) &
#   FRONTEND_PID=$!
#   launcher_fork_done
# Cleanup then adopts a `$!` that moved since the mark, as extension_services.sh
# does for the companion controller.
launcher_fork_mark() {
  extension_services_last_async
  LAUNCHER_PRIOR_ASYNC="$EXTENSION_SERVICES_LAST_ASYNC"
  LAUNCHER_FORK_VAR="$1"
}

launcher_fork_done() {
  unset LAUNCHER_FORK_VAR
}

launcher_adopt_forked_child() {
  [[ -n "${LAUNCHER_FORK_VAR:-}" ]] || return 0
  [[ -z "${!LAUNCHER_FORK_VAR:-}" ]] || return 0
  extension_services_last_async
  if [[ -n "$EXTENSION_SERVICES_LAST_ASYNC" \
    && "$EXTENSION_SERVICES_LAST_ASYNC" != "${LAUNCHER_PRIOR_ASYNC:-}" ]]; then
    printf -v "$LAUNCHER_FORK_VAR" '%s' "$EXTENSION_SERVICES_LAST_ASYNC"
  fi
  unset LAUNCHER_FORK_VAR
}

launcher_cleanup() {
  local status=$? pid alive deadline polls=0
  # First thing after capturing the status: a further cancellation must not kill
  # the launcher half way through this, it only asks the grace wait to end now.
  LAUNCHER_ESCALATED=""
  trap 'LAUNCHER_ESCALATED=1' INT TERM HUP
  trap - EXIT
  launcher_adopt_forked_child

  if [[ -z "${BACKEND_PID:-}" && -z "${FRONTEND_PID:-}" ]]; then
    extension_services_cleanup || status=1
    exit "$status"
  fi

  # Both services are direct children (the launch subshell execs into the real
  # process). Signal them together so one slow shutdown cannot consume a full
  # grace window before the other even receives SIGTERM.
  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done

  deadline=$((SECONDS + ${START_CLEANUP_GRACE_SECONDS:-10}))
  while true; do
    alive=0
    for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
      [[ -n "$pid" ]] || continue
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    [[ "$alive" == "0" || "$SECONDS" -ge "$deadline" || -n "$LAUNCHER_ESCALATED" ]] && break
    if [[ -n "${_SCRIPT_TEST_CLEANUP_MAX_POLLS:-}" \
      && "$polls" -ge "${_SCRIPT_TEST_CLEANUP_MAX_POLLS}" ]]; then
      break
    fi
    sleep "${_SCRIPT_TEST_CLEANUP_POLL_INTERVAL_SECONDS:-1}"
    polls=$((polls + 1))
  done

  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      echo "startup child PID $pid did not stop after SIGTERM; sending SIGKILL" >&2
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${BACKEND_PID:-}" "${FRONTEND_PID:-}"; do
    [[ -n "$pid" ]] || continue
    # A signal that arrives meanwhile (only noted by the trap above) cuts `wait`
    # short with a status above 128 while the child is still there: wait again, so
    # the children are gone before the companions are stopped.
    while ! wait "$pid" 2>/dev/null; do
      kill -0 "$pid" 2>/dev/null || break
    done
  done
  extension_services_cleanup || status=1
  exit "$status"
}

launcher_install_traps() {
  trap launcher_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
}
