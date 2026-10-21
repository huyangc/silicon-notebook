#!/usr/bin/env python3
"""Backend lint gate: ruff against a baseline that may only shrink.

    python scripts/check_backend_lint.py           # the gate (contracts lane)
    python scripts/check_backend_lint.py --prune   # lower stale baseline entries

The gate runs ``ruff check --config ruff.toml`` (the version pinned in
``backend/requirements-dev.txt``) over ``ROOTS`` and counts violations per
(file, rule). ``scripts/backend_lint_baseline.json`` records the violations
that predate the gate, in the same shape. It fails when

* a (file, rule) count exceeds its baseline entry (a new violation);
* a baseline entry exceeds what still occurs (stale: run ``--prune``);
* the baseline names a file ruff does not lint, a rule in ``NEVER_BASELINED``,
  or a count that is not a positive integer;
* ruff does not lint exactly the Python files git sees under ``ROOTS``
  (tracked or untracked-but-not-ignored), minus ``EXCLUDED``;
* the installed ruff is not the pinned one, or ruff itself fails.

``--prune`` lowers each baseline entry to what still occurs and drops entries
that reached zero. It never raises an entry or adds one: with any new
violation present it writes nothing and fails. Per-rule totals are pinned
separately in ``backend/tests/test_backend_lint_gate.py`` (lower them in the
same change). A new violation is fixed, not baselined.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "ruff.toml"
BASELINE = "scripts/backend_lint_baseline.json"
DEV_LOCK = "backend/requirements-dev.txt"
ROOTS = ("backend/app", "backend/tests", "scripts")
# Python files under ROOTS that are deliberately not linted. Empty: nothing in
# ROOTS is generated Python.
EXCLUDED: tuple[str, ...] = ()
# Rules whose violations are always real bugs: they are fixed, never recorded.
# ruff reports a parse failure as ``invalid-syntax``; a diagnostic with no code
# at all is counted under the same name.
NEVER_BASELINED = frozenset({"invalid-syntax", "E902", "F821", "F822", "F823"})

Counts = dict[str, dict[str, int]]


class GateError(Exception):
    pass


def pinned_ruff_version(root: Path = ROOT) -> str:
    match = re.search(r"^ruff==(\S+)", (root / DEV_LOCK).read_text(encoding="utf-8"), re.M)
    if match is None:
        raise GateError(f"{DEV_LOCK} pins no ruff version")
    return match.group(1)


def _ruff(root: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--config", str(root / CONFIG),
         "--no-cache", *args, *ROOTS],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise GateError(
            f"ruff exited {result.returncode}:\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def expected_files(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *ROOTS],
        cwd=root, capture_output=True, text=True, check=True,
    )
    files = {line for line in result.stdout.splitlines()
             if line.endswith(".py") and (root / line).is_file()}
    return sorted(files - set(EXCLUDED))


def linted_files(root: Path = ROOT) -> list[str]:
    out = _ruff(root, "--show-files")
    return sorted(str(Path(line.strip()).resolve().relative_to(root))
                  for line in out.splitlines() if line.strip())


def rule_of(diagnostic: dict) -> str:
    return diagnostic.get("code") or "invalid-syntax"


def current_counts(diagnostics: list[dict], root: Path = ROOT) -> Counts:
    counter: Counter[tuple[str, str]] = Counter()
    for diagnostic in diagnostics:
        relative = str(Path(diagnostic["filename"]).resolve().relative_to(root))
        counter[(relative, rule_of(diagnostic))] += 1
    counts: Counts = {}
    for (relative, rule), count in sorted(counter.items()):
        counts.setdefault(relative, {})[rule] = count
    return counts


def baseline_problems(baseline: Counts, linted: set[str]) -> list[str]:
    problems = []
    for relative, rules in sorted(baseline.items()):
        if relative not in linted:
            problems.append(f"{relative}: baseline names a file ruff does not lint")
        if not isinstance(rules, dict) or not rules:
            problems.append(f"{relative}: baseline entry must be a non-empty rule map")
            continue
        for rule, count in sorted(rules.items()):
            if rule in NEVER_BASELINED:
                problems.append(f"{relative} {rule}: this rule is never baselined; fix it")
            if type(count) is not int or count <= 0:
                problems.append(f"{relative} {rule}: count must be a positive integer")
    return problems


Cell = tuple[str, str, int, int]  # (file, rule, current count, baseline count)


def compare(baseline: Counts, current: Counts) -> tuple[list[Cell], list[Cell]]:
    """Return (new, stale): cells over their baseline and cells under it."""
    new, stale = [], []
    for relative in sorted(set(baseline) | set(current)):
        allowed = baseline.get(relative, {})
        actual = current.get(relative, {})
        for rule in sorted(set(allowed) | set(actual)):
            have, want = actual.get(rule, 0), allowed.get(rule, 0)
            if have > want:
                new.append((relative, rule, have, want))
            elif have < want:
                stale.append((relative, rule, have, want))
    return new, stale


def pruned(baseline: Counts, current: Counts) -> Counts:
    result: Counts = {}
    for relative, rules in sorted(baseline.items()):
        kept = {rule: min(count, current.get(relative, {}).get(rule, 0))
                for rule, count in sorted(rules.items())}
        kept = {rule: count for rule, count in kept.items() if count > 0}
        if kept:
            result[relative] = kept
    return result


def dump_baseline(baseline: Counts) -> str:
    return json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _locations(diagnostics: list[dict], cells: set[tuple[str, str]], root: Path) -> list[str]:
    lines = []
    for diagnostic in diagnostics:
        relative = str(Path(diagnostic["filename"]).resolve().relative_to(root))
        if (relative, rule_of(diagnostic)) in cells:
            location = diagnostic.get("location") or {}
            lines.append(f"  {relative}:{location.get('row')}:{location.get('column')}: "
                         f"{rule_of(diagnostic)} {diagnostic.get('message', '')}")
    return lines


def run(root: Path = ROOT, *, prune: bool = False) -> int:
    pinned = pinned_ruff_version(root)
    installed = metadata.version("ruff")
    if installed != pinned:
        raise GateError(f"ruff {installed} is installed; {DEV_LOCK} pins {pinned}")
    expected = expected_files(root)
    linted = linted_files(root)
    if not expected:
        raise GateError(f"no Python files found under {', '.join(ROOTS)}")
    if linted != expected:
        missing = sorted(set(expected) - set(linted))
        extra = sorted(set(linted) - set(expected))
        raise GateError("ruff does not lint exactly the files under the gate's roots:\n"
                        + "".join(f"  not linted: {path}\n" for path in missing)
                        + "".join(f"  unexpected: {path}\n" for path in extra))
    baseline_path = root / BASELINE
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = baseline_problems(baseline, set(linted))
    if problems:
        raise GateError("invalid baseline:\n" + "".join(f"  {p}\n" for p in problems))
    diagnostics = json.loads(_ruff(root, "--output-format", "json", "--exit-zero"))
    current = current_counts(diagnostics, root)
    new, stale = compare(baseline, current)
    if new:
        print("[backend-lint] new violations (fix them; the baseline only shrinks):", file=sys.stderr)
        for relative, rule, have, want in new:
            print(f"  {relative} {rule}: {have} > baseline {want}", file=sys.stderr)
        cells = {(relative, rule) for relative, rule, _, _ in new}
        for line in _locations(diagnostics, cells, root):
            print(line, file=sys.stderr)
        return 1
    if prune:
        if stale:
            baseline_path.write_text(dump_baseline(pruned(baseline, current)), encoding="utf-8")
            print(f"[backend-lint] lowered {len(stale)} baseline entries; lower the per-rule"
                  " totals in backend/tests/test_backend_lint_gate.py to match")
        else:
            print("[backend-lint] baseline already matches")
        return 0
    if stale:
        print("[backend-lint] baseline entries larger than what still occurs"
              " (run scripts/check_backend_lint.py --prune):", file=sys.stderr)
        for relative, rule, have, want in stale:
            print(f"  {relative} {rule}: {have} < baseline {want}", file=sys.stderr)
        return 1
    total = sum(sum(rules.values()) for rules in baseline.values())
    print(f"[backend-lint] OK: {len(linted)} files, {total} baselined violations, none new")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prune", action="store_true",
                        help="lower stale baseline entries to what still occurs")
    args = parser.parse_args(argv)
    try:
        return run(prune=args.prune)
    except GateError as exc:
        print(f"[backend-lint:error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
