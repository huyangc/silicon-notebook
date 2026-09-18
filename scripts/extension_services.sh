#!/usr/bin/env bash
# Sourced by launchers. Only a receipt from this launch may roll back services.
# Callers provide ROOT_DIR/PYTHON_BIN; no application state is read on stop.

extension_services_start() {
  EXTENSION_SERVICES_RECEIPT="$(mktemp "${TMPDIR:-/tmp}/silicon-extension-start.XXXXXX")"
  PYTHONPATH="${INHERITED_PYTHONPATH-${PYTHONPATH-}}${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" "$ROOT_DIR/scripts/extension_services.py" start \
    --receipt "$EXTENSION_SERVICES_RECEIPT" &
  EXTENSION_SERVICES_CONTROLLER_PID=$!
  # Bash can run cancellation traps while waiting for a background child; it
  # defers them for a foreground external command until that command finishes.
  local status=0
  wait "$EXTENSION_SERVICES_CONTROLLER_PID" || status=$?
  unset EXTENSION_SERVICES_CONTROLLER_PID
  return "$status"
}

extension_services_cleanup() {
  if [[ -n "${EXTENSION_SERVICES_CONTROLLER_PID:-}" ]]; then
    kill -TERM "$EXTENSION_SERVICES_CONTROLLER_PID" 2>/dev/null || true
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
