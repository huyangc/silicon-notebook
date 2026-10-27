#!/usr/bin/env python3
"""Backend lint gate: ruff against a baseline that may only shrink.

    python scripts/check_backend_lint.py                  # the gate (contracts lane)
    python scripts/check_backend_lint.py --prune          # lower stale baseline entries
    python scripts/check_backend_lint.py --rename OLD NEW # carry a renamed file's entries

The gate runs ``ruff check --config ruff.toml`` (the version pinned in
``backend/requirements-dev.txt``) over ``ROOTS`` and counts violations per
(file, rule). ``scripts/backend_lint_baseline.json`` records the violations
that predate the gate, in the same shape. It fails when

* a (file, rule) count exceeds its baseline entry (a new violation);
* a baseline entry exceeds what still occurs, or names a file that is no
  longer linted (stale: run ``--prune``, or ``--rename`` after a rename, then
  lower the pinned totals);
* the baseline names a rule in ``NEVER_BASELINED`` or a count that is not a
  positive integer;
* ruff does not lint exactly the Python files git sees under ``ROOTS``
  (tracked or untracked-but-not-ignored, read NUL-separated so any file name
  works), minus ``EXCLUDED``; a Python file under ``ROOTS`` that is a symbolic
  link is refused outright, and so is a ruff configuration file under them;
* a comment directive suppresses a gated rule beyond ``SUPPRESSIONS`` (per
  rule, zero slack, may only fall), or is one of the forbidden kinds: a blanket
  ``# noqa``, a file-level ``# ruff:`` / ``# flake8:`` directive, or any
  ``# isort:`` action comment;
* the installed ruff is not the pinned one, ruff itself fails, or git is
  unusable (exit 2 with a readable message for every environment problem).

``--prune`` lowers each baseline entry to what still occurs and drops entries
that reached zero, including those of deleted files. ``--rename OLD NEW`` moves
the entries of a file that was renamed (OLD no longer linted, NEW linted and
not yet in the baseline), capped at what NEW still has. Neither ever raises an
entry or adds debt: with any new violation present they write nothing and
fail. Per-rule totals are pinned separately in
``backend/tests/test_backend_lint_gate.py`` (lower them in the same change).
A new violation is fixed, not baselined.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "ruff.toml"
BASELINE = "scripts/backend_lint_baseline.json"
DEV_LOCK = "backend/requirements-dev.txt"
CEILING_FILE = "backend/tests/test_backend_lint_gate.py"
INSTALL_HINT = ("install the gate tools: python -m pip install --require-hashes"
                " -r backend/requirements.txt -r backend/requirements-dev.txt")
ROOTS = ("backend/app", "backend/tests", "scripts")
# Python files under ROOTS that are deliberately not linted. Empty: nothing in
# ROOTS is generated Python.
EXCLUDED: tuple[str, ...] = ()
# Rules whose violations are always real bugs: they are fixed, never recorded.
# ruff reports a parse failure as ``invalid-syntax``; a diagnostic with no code
# at all is counted under the same name.
NEVER_BASELINED = frozenset({"invalid-syntax", "E902", "F821", "F822", "F823"})
# Line suppressions that name a gated rule by its code, per rule. They
# predate the gate: pytest fixtures imported for injection, re-exports and
# side-effect imports. Only ever lowered; pinned again in CEILING_FILE.
SUPPRESSIONS = {
    "F401": {"count": 65, "files": 55},
    "F811": {"count": 99, "files": 13},
}

# File names ruff reads its configuration from.
NESTED_CONFIGS = frozenset({"ruff.toml", ".ruff.toml", "pyproject.toml"})

Counts = dict[str, dict[str, int]]

# Spelled in pieces so this module carries no directive-shaped comment text.
_NOQA = "no" + "qa"
_LINE_DIRECTIVE = re.compile(
    rf"#\s*{_NOQA}(?![A-Za-z0-9_])(?P<colon>\s*:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*))?",
    re.IGNORECASE,
)
# ruff/flake8 file-level exemptions, and every isort action comment (skip,
# skip_file, off/on, split): each one hides or re-cuts import order.
_FORBIDDEN_DIRECTIVE = re.compile(r"#\s*(?:ruff|flake8|isort)\s*:", re.IGNORECASE)


class GateError(Exception):
    pass


def pinned_ruff_version(root: Path = ROOT) -> str:
    match = re.search(r"^ruff==(\S+)", (root / DEV_LOCK).read_text(encoding="utf-8"), re.M)
    if match is None:
        raise GateError(f"{DEV_LOCK} pins no ruff version")
    return match.group(1)


def installed_ruff_version() -> str:
    try:
        return metadata.version("ruff")
    except metadata.PackageNotFoundError:
        raise GateError(f"ruff is not installed in {sys.executable}; {INSTALL_HINT}") from None


def selected_rules(root: Path = ROOT) -> list[str]:
    with (root / CONFIG).open("rb") as handle:
        return list(tomllib.load(handle)["lint"]["select"])


def gated(code: str, select: list[str]) -> bool:
    """True when ``select`` covers ``code`` the way ruff selects: a bare linter
    prefix (``F``, ``PLE``, ``I``) needs digits next, anything else is a code
    prefix (``E9``, ``E722``, ``B006``)."""
    for selector in select:
        if selector.isalpha():
            if re.fullmatch(rf"{selector}[0-9]+", code):
                return True
        elif code.startswith(selector):
            return True
    return False


def _run(args: list[str], root: Path, what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=root, capture_output=True)
    except FileNotFoundError:
        raise GateError(f"{what}: {args[0]} not found on PATH") from None


def _ruff(root: Path, *args: str) -> str:
    result = _run([sys.executable, "-m", "ruff", "check", "--config", str(root / CONFIG),
                   "--no-cache", *args, *ROOTS], root, "ruff")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise GateError(f"ruff exited {result.returncode}:\n{detail}")
    return result.stdout.decode("utf-8")


def parse_ls_files(stdout: bytes) -> list[str]:
    """``git ls-files -z`` output: NUL-separated, never quoted."""
    return [item.decode("utf-8", "surrogateescape") for item in stdout.split(b"\0") if item]


def expected_files(root: Path = ROOT) -> list[str]:
    result = _run(["git", "-c", "core.quotePath=false", "ls-files", "-z", "--cached",
                   "--others", "--exclude-standard", "--", *ROOTS], root, "listing files")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"git ls-files failed in {root} (the gate runs in a git checkout):\n{detail}")
    listed = parse_ls_files(result.stdout)
    configs = sorted(path for path in listed if path.rsplit("/", 1)[-1] in NESTED_CONFIGS)
    if configs:
        raise GateError("ruff configuration under the gate's roots would override ruff.toml"
                        " for its subtree; remove it:\n" + "".join(f"  {path}\n" for path in configs))
    files = [path for path in listed if path.endswith(".py")]
    links = sorted(path for path in files if (root / path).is_symlink())
    if links:
        raise GateError("Python files under the gate's roots must be regular files, not"
                        " symbolic links (replace the link with the file):\n"
                        + "".join(f"  {path}\n" for path in links))
    return sorted({path for path in files if (root / path).is_file()} - set(EXCLUDED))


def relative_path(path: str, root: Path = ROOT) -> str:
    """``path`` relative to ``root`` without following symbolic links."""
    absolute = Path(os.path.abspath(os.path.join(root, path)))
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        raise GateError(f"ruff reported a file outside the repository: {path}") from None


def linted_files(root: Path = ROOT) -> list[str]:
    out = _ruff(root, "--show-files")
    return sorted(relative_path(line.strip(), root) for line in out.splitlines() if line.strip())


def rule_of(diagnostic: dict) -> str:
    return diagnostic.get("code") or "invalid-syntax"


def current_counts(diagnostics: list[dict], root: Path = ROOT) -> Counts:
    counter: Counter[tuple[str, str]] = Counter()
    for diagnostic in diagnostics:
        counter[(relative_path(diagnostic["filename"], root), rule_of(diagnostic))] += 1
    counts: Counts = {}
    for (relative, rule), count in sorted(counter.items()):
        counts.setdefault(relative, {})[rule] = count
    return counts


def directive_findings(text: str, select: list[str]) -> tuple[list[str], list[str]]:
    """(gated codes named by line suppressions, forbidden directives) in the
    comments of one Python source. Only comment tokens count."""
    codes_found: list[str] = []
    forbidden: list[str] = []
    lowered = text.lower()
    if not any(word in lowered for word in (_NOQA, "ruff", "flake8", "isort")):
        return codes_found, forbidden
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return codes_found, forbidden  # ruff reports the file as invalid-syntax
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        comment = token.string
        if _FORBIDDEN_DIRECTIVE.search(comment):
            forbidden.append(f"suppression directive {comment!r}")
        for match in _LINE_DIRECTIVE.finditer(comment):
            codes = [code.upper() for code in re.split(r"[\s,]+", (match.group("codes") or "").strip())
                     if code]
            if not codes:
                forbidden.append(f"blanket line directive {comment!r}")
            codes_found.extend(code for code in codes if gated(code, select))
    return codes_found, forbidden


def suppression_totals(root: Path, files: list[str],
                       select: list[str]) -> tuple[dict[str, dict[str, int]], list[str]]:
    per_rule: Counter[str] = Counter()
    files_per_rule: Counter[str] = Counter()
    forbidden = []
    for relative in files:
        codes, bad = directive_findings((root / relative).read_text(encoding="utf-8"), select)
        per_rule.update(codes)
        files_per_rule.update(set(codes))
        forbidden.extend(f"{relative}: {item}" for item in bad)
    totals = {rule: {"count": per_rule[rule], "files": files_per_rule[rule]} for rule in sorted(per_rule)}
    return totals, forbidden


def suppression_problems(actual: dict[str, dict[str, int]]) -> list[str]:
    problems = []
    for rule in sorted(set(actual) | set(SUPPRESSIONS)):
        have = actual.get(rule, {"count": 0, "files": 0})
        want = SUPPRESSIONS.get(rule, {"count": 0, "files": 0})
        if have["count"] > want["count"] or have["files"] > want["files"]:
            problems.append(f"{rule}: {have} suppressed, more than {want}; fix the code"
                            " instead of suppressing the finding")
        elif have != want:
            problems.append(f"{rule}: {have} suppressed, fewer than {want}; lower SUPPRESSIONS"
                            f" in scripts/check_backend_lint.py and SUPPRESSION_CEILING in"
                            f" {CEILING_FILE} to match")
    return problems


def baseline_problems(baseline: Counts, linted: set[str], *, prune: bool = False) -> list[str]:
    """Structural problems. ``prune`` tolerates entries of files no longer
    linted (deleted or renamed): pruning drops them."""
    problems = []
    for relative, rules in sorted(baseline.items()):
        if relative not in linted and not prune:
            problems.append(f"{relative}: baseline names a file ruff does not lint. Deleted:"
                            " run --prune. Renamed: run --rename OLD NEW")
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


def renamed(baseline: Counts, current: Counts, linted: set[str], old: str, new: str) -> Counts:
    """Move OLD's entries to NEW, each capped at what NEW still has."""
    if old not in baseline:
        raise GateError(f"--rename: {old} has no baseline entries")
    if old in linted:
        raise GateError(f"--rename: {old} is still linted; it was not renamed")
    if new not in linted:
        raise GateError(f"--rename: {new} is not a file the gate lints")
    if new in baseline:
        raise GateError(f"--rename: {new} already has baseline entries")
    result = {path: dict(rules) for path, rules in baseline.items() if path != old}
    moved = {rule: min(count, current.get(new, {}).get(rule, 0))
             for rule, count in sorted(baseline[old].items())}
    moved = {rule: count for rule, count in moved.items() if count > 0}
    if moved:
        result[new] = moved
    return dict(sorted(result.items()))


def ceiling_deltas(before: Counts, after: Counts) -> dict[str, tuple[int, int]]:
    """Per rule, how far the pinned (count, files) totals fall from ``before`` to ``after``."""
    def totals(counts: Counts) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {}
        for rules in counts.values():
            for rule, count in rules.items():
                entry = result.setdefault(rule, [0, 0])
                entry[0] += count
                entry[1] += 1
        return result
    old, new = totals(before), totals(after)
    deltas = {}
    for rule in sorted(old):
        count, files = new.get(rule, [0, 0])
        if (count, files) != tuple(old[rule]):
            deltas[rule] = (old[rule][0] - count, old[rule][1] - files)
    return deltas


def _ceiling_advice(before: Counts, after: Counts) -> list[str]:
    return [f"  {rule}: count -{count}, files -{files}"
            for rule, (count, files) in ceiling_deltas(before, after).items()]


def dump_baseline(baseline: Counts) -> str:
    return json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _locations(diagnostics: list[dict], cells: set[tuple[str, str]], root: Path) -> list[str]:
    lines = []
    for diagnostic in diagnostics:
        relative = relative_path(diagnostic["filename"], root)
        if (relative, rule_of(diagnostic)) in cells:
            location = diagnostic.get("location") or {}
            lines.append(f"  {relative}:{location.get('row')}:{location.get('column')}: "
                         f"{rule_of(diagnostic)} {diagnostic.get('message', '')}")
    return lines


def _write_lowered(path: Path, before: Counts, after: Counts, what: str) -> None:
    path.write_text(dump_baseline(after), encoding="utf-8")
    print(f"[backend-lint] {what}; in the same change lower BASELINE_CEILING in {CEILING_FILE} by:")
    for line in _ceiling_advice(before, after) or ["  (nothing: the totals are unchanged)"]:
        print(line)


def run(root: Path = ROOT, *, prune: bool = False, rename: tuple[str, str] | None = None) -> int:
    pinned = pinned_ruff_version(root)
    installed = installed_ruff_version()
    if installed != pinned:
        raise GateError(f"ruff {installed} is installed; {DEV_LOCK} pins {pinned}; {INSTALL_HINT}")
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
    select = selected_rules(root)
    suppressions, forbidden = suppression_totals(root, linted, select)
    if forbidden:
        print("[backend-lint] forbidden suppression directives (fix the finding instead):",
              file=sys.stderr)
        for line in forbidden:
            print(f"  {line}", file=sys.stderr)
        return 1
    problems = suppression_problems(suppressions)
    if problems:
        print("[backend-lint] line suppressions of gated rules changed:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    baseline_path = root / BASELINE
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = baseline_problems(baseline, set(linted), prune=prune or rename is not None)
    if problems:
        raise GateError("invalid baseline:\n" + "".join(f"  {p}\n" for p in problems))
    diagnostics = json.loads(_ruff(root, "--output-format", "json", "--exit-zero"))
    current = current_counts(diagnostics, root)
    proposed = renamed(baseline, current, set(linted), *rename) if rename else baseline
    new, stale = compare(proposed, current)
    if new:
        print("[backend-lint] new violations (fix them; the baseline only shrinks):", file=sys.stderr)
        for relative, rule, have, want in new:
            print(f"  {relative} {rule}: {have} > baseline {want}", file=sys.stderr)
        cells = {(relative, rule) for relative, rule, _, _ in new}
        for line in _locations(diagnostics, cells, root):
            print(line, file=sys.stderr)
        return 1
    if rename:
        if stale:
            print(f"[backend-lint] {rename[1]} now has fewer findings than {rename[0]} had;"
                  " run --prune afterwards", file=sys.stderr)
        _write_lowered(baseline_path, baseline, proposed, f"moved {rename[0]} -> {rename[1]}")
        return 0
    if prune:
        if stale:
            _write_lowered(baseline_path, baseline, pruned(baseline, current),
                           f"lowered {len(stale)} baseline entries")
        else:
            print("[backend-lint] baseline already matches")
        return 0
    if stale:
        print("[backend-lint] baseline entries larger than what still occurs (a fixed"
              " violation, a sorted import block, a deleted file). Run"
              " `python scripts/check_backend_lint.py --prune`, then lower"
              f" BASELINE_CEILING in {CEILING_FILE} by the amounts it prints:", file=sys.stderr)
        for relative, rule, have, want in stale:
            print(f"  {relative} {rule}: {have} < baseline {want}", file=sys.stderr)
        return 1
    total = sum(sum(rules.values()) for rules in baseline.values())
    print(f"[backend-lint] OK: {len(linted)} files, {total} baselined violations, none new")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prune", action="store_true",
                        help="lower stale baseline entries to what still occurs")
    action.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"),
                        help="move the entries of a renamed file to its new path")
    args = parser.parse_args(argv)
    try:
        return run(prune=args.prune, rename=tuple(args.rename) if args.rename else None)
    except GateError as exc:
        print(f"[backend-lint:error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
