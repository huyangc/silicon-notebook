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


def main() -> int:
    # The supervisor creates this guard as a session leader. Keeping it alive
    # until the final group kill prevents group-id reuse during cleanup.
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
        child = subprocess.Popen(
            payload["command"], cwd=payload["cwd"], env=payload["env"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        atomic_json(state, {"state": "running"})
        while not stopping and os.getppid() == owner:
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
