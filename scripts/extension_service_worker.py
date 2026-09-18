"""Private process-group guard; commands must run in the foreground."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

POLL_SECONDS = 0.05


def cleanup_witness(shutdown_timeout: float) -> tuple[int, int]:
    """Keep group membership and inherited leases if the leader is killed.

    The pipe writer belongs only to the guard. EOF therefore proves guard
    death without consulting a saved PID; this witness can safely signal the
    group it still belongs to. The guard also watches witness death, so losing
    either member closes the session instead of leaving it unprotected.
    """
    reader, writer = os.pipe()
    try:
        witness = os.fork()
    except BaseException:
        os.close(reader)
        os.close(writer)
        raise
    if witness:
        os.close(reader)
        return witness, writer
    os.close(writer)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        while os.read(reader, 1):
            pass
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(shutdown_timeout)
        os.killpg(os.getpgrp(), signal.SIGKILL)
    finally:
        os._exit(1)


def main() -> int:
    # The supervisor creates this guard as a session leader. A same-group
    # witness retains membership and leases if the guard itself is killed.
    owner = int(sys.argv[1])
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    payload = json.load(sys.stdin)
    from extension_service_runtime import atomic_json

    state = Path(payload["worker_state"])
    try:
        witness, witness_writer = cleanup_witness(payload["shutdown_timeout_seconds"])
        child = subprocess.Popen(
            payload["command"], cwd=payload["cwd"], env=payload["env"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Internal live identities only; no stored PID authorizes signalling.
        atomic_json(state, {"state": "running", "witness_pid": witness})
        while not stopping and os.getppid() == owner:
            if os.waitpid(witness, os.WNOHANG)[0]:
                atomic_json(state, {"state": "failed"})
                break
            if child.poll() is not None:
                atomic_json(state, {"state": "exited", "returncode": child.returncode})
                break
            time.sleep(POLL_SECONDS)
    except Exception:
        atomic_json(state, {"state": "failed"})
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(payload["shutdown_timeout_seconds"])
        os.killpg(os.getpgrp(), signal.SIGKILL)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
