#!/usr/bin/env bash
# Sourced by launchers. Only a receipt from this launch may roll back services.
# Callers provide ROOT_DIR/PYTHON_BIN; no application state is read on stop.

EXTENSION_SERVICES_POLL_SECONDS=0.05

extension_services_start() {
  EXTENSION_SERVICES_RECEIPT="$(mktemp "${TMPDIR:-/tmp}/silicon-extension-start.XXXXXX")"
  PYTHONPATH="${INHERITED_PYTHONPATH-${PYTHONPATH-}}${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" "$ROOT_DIR/scripts/extension_services.py" start \
    --receipt "$EXTENSION_SERVICES_RECEIPT" &
  EXTENSION_SERVICES_CONTROLLER_PID=$!
  # Never block in `wait` while the controller runs: a cancellation that lands
  # between the fork and `wait` (seen with macOS /bin/bash 3.2) stays pending
  # until the waited child exits, i.e. until every companion has started. A
  # trap pending across a foreground command runs as soon as it returns, so a
  # short poll honours a cancellation within one interval wherever it lands.
  while kill -0 "$EXTENSION_SERVICES_CONTROLLER_PID" 2>/dev/null; do
    sleep "$EXTENSION_SERVICES_POLL_SECONDS"
  done
  local status=0
  wait "$EXTENSION_SERVICES_CONTROLLER_PID" || status=$?
  unset EXTENSION_SERVICES_CONTROLLER_PID
  return "$status"
}

extension_services_cleanup() {
  if [[ -n "${EXTENSION_SERVICES_CONTROLLER_PID:-}" ]]; then
    # A TERM that lands after the fork but before the controller has exec'd is
    # caught by the fork's copy of this shell's traps and lost at exec, and a
    # blocking `wait` would then last until every companion has started. So
    # repeat it until the controller is gone; the controller acts on the first
    # cancellation it receives and absorbs the rest.
    while kill -TERM "$EXTENSION_SERVICES_CONTROLLER_PID" 2>/dev/null; do
      sleep "$EXTENSION_SERVICES_POLL_SECONDS"
    done
    wait "$EXTENSION_SERVICES_CONTROLLER_PID" 2>/dev/null || true
    unset EXTENSION_SERVICES_CONTROLLER_PID
  fi
  if [[ -n "${EXTENSION_SERVICES_RECEIPT:-}" ]]; then
    if [[ -s "$EXTENSION_SERVICES_RECEIPT" ]]; then
      "$PYTHON_BIN" "$ROOT_DIR/scripts/extension_services.py" stop \
        --receipt "$EXTENSION_SERVICES_RECEIPT" || return $?
    fi
    rm -f -- "$EXTENSION_SERVICES_RECEIPT"
    unset EXTENSION_SERVICES_RECEIPT
  fi
}

extension_services_handoff() {
  if [[ -n "${EXTENSION_SERVICES_RECEIPT:-}" ]]; then
    rm -f -- "$EXTENSION_SERVICES_RECEIPT"
    unset EXTENSION_SERVICES_RECEIPT
  fi
}

extension_services_stop() {
  "$PYTHON_BIN" "$ROOT_DIR/scripts/extension_services.py" stop
}
