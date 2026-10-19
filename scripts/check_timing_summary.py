#!/usr/bin/env python3
"""Print the G1 gate's timing summary; warn when it is over the local target.

``scripts/check.sh`` runs this after every lane has finished. It prints the
gate's wall time, each lane's seconds and the slowest backend test files and
tests from this run's JUnit report, so every gate log shows what got slower.
Over ``--target`` seconds it prints a prominent warning. It never fails the
gate: the 100 s target (60 s remains the long-term aim) is a local Apple
Silicon target and CI timings are observational only (docs/development.md), so this always exits 0, also when
its inputs are missing or unreadable.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from pathlib import Path

LANES = ("backend", "contracts", "frontend")
TOP = 10


def _lane_seconds(timing_dir: Path) -> list[str]:
    parts = []
    for lane in LANES:
        try:
            line = (timing_dir / f"{lane}.time").read_text(encoding="utf-8").strip()
        except OSError:
            line = ""
        parts.append(line if line.startswith(f"{lane}=") else f"{lane}=missing")
    return parts


def _module_file(classname: str) -> str:
    """``tests.test_x.TestY`` -> ``tests/test_x.py`` (the module, not the class)."""
    parts = classname.split(".")
    for index, part in enumerate(parts):
        if part.startswith("test_"):
            return "/".join(parts[: index + 1]) + ".py"
    return "/".join(parts) + ".py"


def _junit_rows(junit: Path, started_at: float) -> list[tuple[str, str, float]] | str:
    try:
        if junit.stat().st_mtime < started_at:
            return "not written by this run"
        root = ElementTree.parse(junit).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        return f"unreadable ({type(exc).__name__})"
    rows = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        try:
            seconds = float(case.get("time", "0") or 0)
        except ValueError:
            seconds = 0.0
        rows.append((_module_file(classname), f"{classname}::{case.get('name', '')}", seconds))
    return rows


def summarize(
    wall: int, timing_dir: Path, junit: Path, started_at: float, target: int
) -> str:
    lines = ["", "===== gate timing =====", f"gate_wall={wall} " + " ".join(_lane_seconds(timing_dir))]
    rows = _junit_rows(junit, started_at)
    if isinstance(rows, str):
        lines.append(f"backend junit {junit.name}: {rows}")
    else:
        by_file: dict[str, float] = defaultdict(float)
        for module, _test, seconds in rows:
            by_file[module] += seconds
        lines.append(f"slowest backend test files (summed junit seconds, top {TOP}):")
        for module, seconds in sorted(by_file.items(), key=lambda item: (-item[1], item[0]))[:TOP]:
            lines.append(f"  {seconds:8.2f}s  {module}")
        lines.append(f"slowest backend tests (junit seconds, top {TOP}):")
        for _module, test, seconds in sorted(rows, key=lambda row: (-row[2], row[1]))[:TOP]:
            lines.append(f"  {seconds:8.2f}s  {test}")
    if wall > target:
        banner = "!" * 72
        lines += [
            banner,
            f"WARNING: the G1 gate took {wall} s, over the {target} s local target",
            "(docs/development.md). This is a warning only and does not fail the",
            "gate; the slowest files and tests above show where the time went.",
            banner,
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wall", type=int, required=True)
    parser.add_argument("--timing-dir", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--started-at", type=float, required=True)
    parser.add_argument("--target", type=int, default=100)
    try:
        args = parser.parse_args(argv)
        sys.stdout.write(
            summarize(args.wall, args.timing_dir, args.junit, args.started_at, args.target)
        )
    except BaseException as exc:  # noqa: BLE001 - a summary never fails the gate
        if isinstance(exc, KeyboardInterrupt):
            raise
        sys.stdout.write(f"\ngate timing summary unavailable ({type(exc).__name__})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
