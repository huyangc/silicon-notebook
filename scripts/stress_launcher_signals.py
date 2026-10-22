#!/usr/bin/env python3
"""Stress the launchers' cancellation next to the companion-controller fork (not in CI).

Two macOS ``/bin/bash`` 3.2 races sit in a sub-millisecond window next to the
fork in ``scripts/extension_services.sh``, and no deterministic test can aim
at them:

* a trap pending when ``wait`` blocks stays pending until the waited child
  exits, so ``extension_services_start`` must poll the controller and never
  block in ``wait``;
* a TERM delivered between the fork and the controller's exec is lost, so
  ``extension_services_cleanup`` must repeat its TERM until the controller
  exits.

It also catches a third, frequent path: a pending trap that runs between the
fork and ``EXTENSION_SERVICES_CONTROLLER_PID=$!``, after which cleanup must
find the controller through ``$!`` or the launcher exits and leaves it running.

Each attempt starts a real launcher in a fake checkout
(``backend/tests/launcher_checkout_testkit.py``) whose controller would wait
forever. It cancels the launcher 0-4 ms after the launcher creates its
receipt, which mktemp does right before the fork. A launcher still alive
``--held-seconds`` after its cancellation counts as held; a launcher that
exited but left its controller running counts as leaked (the controller is
killed by exact pid). Shards run in parallel processes, one fake checkout each.

    python scripts/stress_launcher_signals.py --load auto
    python scripts/stress_launcher_signals.py --launchers dev.sh --shards 2 --runs 300

Exit status: 0 nothing held or leaked, 1 at least one held or leaked, 2 bad
usage or a broken shard.
The blocking-``wait`` race hits about once in 5000-6000 attempts, so the
default 2 launchers x 4 shards x 1500 attempts catch a reintroduced blocking
``wait`` with roughly 85-90% probability; raise ``--runs`` or repeat the run
for more confidence.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHERS = {
    "dev.sh": ("scripts/dev.sh", ()),
    "prod.sh": ("scripts/prod.sh", ()),
    "backend.sh": ("scripts/backend.sh", ("start",)),
    "start.sh": ("start.sh", ()),
}
MAX_OFFSET_SECONDS = 0.004


def run_shard(launcher: str, shard: int, runs: int, seed: str | None, held_seconds: float) -> dict:
    sys.path.insert(0, str(ROOT / "backend"))
    from tests.launcher_checkout_testkit import (
        build_fake_checkout,
        kill_published,
        reap_leaked_controllers,
        spawn,
        stop_fake_children,
    )

    path, args = LAUNCHERS[launcher]
    rng = random.Random(None if seed is None else f"{seed}-{launcher}-{shard}")
    held = []
    leaked = []
    with tempfile.TemporaryDirectory(prefix="stress-launcher-") as base:
        tmp = Path(base)
        root, env = build_fake_checkout(tmp)
        try:
            for attempt in range(runs):
                for leftover in (root / "controller.pid", root / "service-events.jsonl",
                                 *tmp.glob("silicon-extension-start.*")):
                    leftover.unlink(missing_ok=True)
                process = spawn(root, {**env, "WAIT_SERVICE": "1"}, path, *args)
                deadline = time.monotonic() + held_seconds
                # Spin, do not sleep: the receipt appears just before the fork.
                while not any(tmp.glob("silicon-extension-start.*")):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f"{launcher} never created its receipt (attempt {attempt})")
                time.sleep(rng.uniform(0, MAX_OFFSET_SECONDS))
                interruption = rng.choice((signal.SIGTERM, signal.SIGHUP))
                process.send_signal(interruption)
                try:
                    process.wait(timeout=held_seconds)
                except subprocess.TimeoutExpired:
                    held.append([attempt, interruption.name])
                    process.kill()
                    process.wait()
                    kill_published(root / "controller.pid")
                # The launcher waits for its controller before it exits.
                if reap_leaked_controllers(root, 1.0) and not (held and held[-1][0] == attempt):
                    leaked.append([attempt, interruption.name])
        finally:
            stop_fake_children(root)
    return {"launcher": launcher, "shard": shard, "runs": runs, "held": held, "leaked": leaked}


def parse(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--launchers", default="dev.sh,backend.sh",
                        help=f"comma-separated, from {', '.join(LAUNCHERS)} (default: dev.sh,backend.sh)")
    parser.add_argument("--shards", type=int, default=4, help="parallel shards per launcher (default 4)")
    parser.add_argument("--runs", type=int, default=1500, help="attempts per shard (default 1500)")
    parser.add_argument("--load", default="0",
                        help="busy loops to run alongside: a number, or 'auto' for one per core (default 0)")
    parser.add_argument("--seed", help="fix the random offsets and signals")
    parser.add_argument("--held-seconds", type=float, default=20.0,
                        help="a launcher still alive this long after its cancellation is held (default 20)")
    parser.add_argument("--worker", nargs=2, metavar=("LAUNCHER", "SHARD"), help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    launchers = [name.strip() for name in args.launchers.split(",") if name.strip()]
    unknown = [name for name in launchers if name not in LAUNCHERS]
    if unknown or not launchers:
        parser.error(f"unknown launcher(s): {', '.join(unknown) or '(none)'}")
    if args.shards < 1 or args.runs < 1 or args.held_seconds <= 0:
        parser.error("--shards, --runs and --held-seconds must be positive")
    if args.load != "auto" and not (args.load.isdigit()):
        parser.error("--load takes a number or 'auto'")
    args.launcher_names = launchers
    return args


def main(arguments: list[str] | None = None) -> int:
    args = parse(arguments)
    if args.worker:
        launcher, shard = args.worker
        print(json.dumps(run_shard(launcher, int(shard), args.runs, args.seed, args.held_seconds)))
        return 0
    load = (os.cpu_count() or 1) if args.load == "auto" else int(args.load)
    bash = subprocess.run(["/bin/bash", "--version"], capture_output=True, text=True).stdout.splitlines()
    print(f"bash: {bash[0] if bash else '?'}; load: {load} busy loop(s); "
          f"{len(args.launcher_names)} launcher(s) x {args.shards} shard(s) x {args.runs} attempt(s)", flush=True)
    hogs = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(load)]
    try:
        base = [sys.executable, str(Path(__file__).resolve()), "--runs", str(args.runs),
                "--held-seconds", str(args.held_seconds)]
        if args.seed is not None:
            base += ["--seed", args.seed]
        workers = [
            (launcher, shard, subprocess.Popen([*base, "--worker", launcher, str(shard)],
                                               stdout=subprocess.PIPE, text=True))
            for launcher in args.launcher_names for shard in range(args.shards)
        ]
        total = held_total = leaked_total = 0
        failed = False
        for launcher, shard, worker in workers:
            output, _ = worker.communicate()
            if worker.returncode != 0:
                print(f"{launcher} shard {shard}: worker failed (exit {worker.returncode})", flush=True)
                failed = True
                continue
            result = json.loads(output.strip().splitlines()[-1])
            total += result["runs"]
            held_total += len(result["held"])
            leaked_total += len(result["leaked"])
            print(f"{launcher} shard {shard}: {len(result['held'])}/{result['runs']} held {result['held']}, "
                  f"{len(result['leaked'])} leaked {result['leaked']}", flush=True)
    finally:
        for hog in hogs:
            hog.kill()
        for hog in hogs:
            hog.wait()
    print(f"total: {held_total}/{total} cancellations held, {leaked_total}/{total} controllers leaked", flush=True)
    if failed:
        return 2
    return 1 if held_total or leaked_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
