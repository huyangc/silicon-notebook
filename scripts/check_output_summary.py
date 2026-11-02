"""Render a bounded gate report; the original lane logs remain authoritative."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

FAILURE_TAIL_LINES = 80
FAILURE_TAIL_CHARS = 16_000
WALL_TARGET_SECONDS = 100
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RUNNER_SUMMARIES = (
    re.compile(r"^=+ .*(?:\d+ passed|\d+ failed|\d+ errors?).* in .*=+$"),
    re.compile(r"^(?:#|ℹ) (?:tests|pass|fail|cancelled|skipped|todo) \d+$"),
    re.compile(r"^Tests\s+.*\d+ (?:passed|failed|skipped).*$"),
)


def test_counts(log: str) -> list[str]:
    """Return runner-reported counts without guessing from progress output."""
    lines = (ANSI_ESCAPE.sub("", line).strip() for line in log.splitlines())
    return [line for line in lines if any(p.fullmatch(line) for p in RUNNER_SUMMARIES)]


def lane_report(directory: Path, lane: str) -> str:
    log_path = directory / f"{lane}.log"
    exit_path = directory / f"{lane}.exit"
    time_path = directory / f"{lane}.time"
    exit_code = exit_path.read_text().strip() if exit_path.is_file() else "unavailable"
    timing = time_path.read_text().strip() if time_path.is_file() else ""
    name, separator, seconds = timing.partition("=")
    elapsed = f"{seconds}s" if name == lane and separator and seconds.isdigit() else "unavailable"
    log = log_path.read_text(errors="replace") if log_path.is_file() else ""
    counts = test_counts(log)
    report = [
        f"{lane}: exit={exit_code}; elapsed={elapsed}",
        f"  tests (runner-reported): {'; '.join(counts) if counts else 'unavailable'}",
        f"  full log: {log_path}",
    ]
    if exit_code != "0":
        tail = "\n".join(log.splitlines()[-FAILURE_TAIL_LINES:])[-FAILURE_TAIL_CHARS:]
        report.append(
            f"  failure excerpt (last {FAILURE_TAIL_LINES} lines, at most "
            f"{FAILURE_TAIL_CHARS} characters; see full log above):"
        )
        report.append(tail or "  No output captured; lane did not complete successfully.")
    return "\n".join(report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--lane", action="append", required=True)
    parser.add_argument("--wall", type=int, required=True)
    parser.add_argument("--gate-exit", type=int, required=True)
    args = parser.parse_args()
    for lane in args.lane:
        print(lane_report(args.log_dir, lane))
    print(f"gate: exit={args.gate_exit}; wall={args.wall}s; logs retained: {args.log_dir}")
    if args.wall > WALL_TARGET_SECONDS:
        print(f"WARNING: gate wall time exceeds {WALL_TARGET_SECONDS}s local target")


if __name__ == "__main__":
    main()
