"""Guard for the backend lint gate: what ``scripts/check_backend_lint.py`` cannot see.

The gate (contracts lane) runs ruff and compares per-(file, rule) counts with
``scripts/backend_lint_baseline.json``: a cell over its entry is a new
violation, a cell under it is stale. This module covers the holes that leaves:

1. Per-rule totals and file counts are pinned in ``BASELINE_CEILING`` with zero
   slack, so rewriting the baseline to absorb a new violation turns this red.
   The pin is per rule: one violation fixed in file A and one added in file B
   under the same rule keeps every number, and only review of the baseline
   diff catches that.
2. The rule set cannot be loosened: ``ruff.toml`` must equal ``EXPECTED_CONFIG``
   exactly (no ``ignore``, ``per-file-ignores``, ``exclude``, preview or
   extend keys), the gate's roots and exclusions are pinned, and no other ruff
   configuration sits under the roots.
3. Line and file suppressions cannot absorb debt: a blanket line directive,
   every file-level directive and every ``isort:`` action comment are
   forbidden, and line directives that name a gated rule are pinned per rule
   in ``SUPPRESSION_CEILING`` (zero slack, may only fall).
4. Rules in ``NEVER_BASELINED`` (``F821`` undefined name and friends) are
   never recorded: such a violation is a bug and is fixed on the spot.

Unit tests at the bottom drive the gate's comparison on fixtures; the one
real-tree run is the contracts lane's.
"""
from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import tokenize
import tomllib
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "check_backend_lint", ROOT / "scripts" / "check_backend_lint.py"
)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, gate)
_spec.loader.exec_module(gate)


#: Per-rule totals and number of files in the baseline. Only ever lowered, and
#: always equal to the baseline (lower both in the same change).
BASELINE_CEILING = {
    "B004": {"count": 1, "files": 1},
    "B015": {"count": 2, "files": 1},
    "B017": {"count": 13, "files": 8},
    "B018": {"count": 2, "files": 2},
    "B039": {"count": 1, "files": 1},
    "F401": {"count": 242, "files": 105},
    "F402": {"count": 2, "files": 1},
    "F403": {"count": 1, "files": 1},
    "F811": {"count": 402, "files": 38},
    "F841": {"count": 9, "files": 9},
    # Import order (I001, one per unsorted import block). Recorded, never
    # auto-fixed: no import was reordered when the rule was turned on.
    "I001": {"count": 1395, "files": 1009},
}

#: Line suppressions that name a gated rule, per rule. These predate the gate
#: (they were written for flake8-era tooling): pytest fixtures imported for
#: injection, re-exports and side-effect imports. Only ever lowered.
SUPPRESSION_CEILING = {
    "F401": {"count": 66, "files": 56},
    "F811": {"count": 99, "files": 13},
}

EXPECTED_CONFIG = {
    "target-version": "py313",
    "line-length": 120,
    "src": ["backend", "scripts"],
    "lint": {
        "select": [
            "F", "E9", "E722", "PLE", "I",
            "B002", "B003", "B004", "B005", "B006", "B012", "B013", "B014", "B015",
            "B016", "B017", "B018", "B019", "B020", "B021", "B022", "B025", "B026",
            "B029", "B030", "B031", "B032", "B033", "B034", "B035", "B039",
        ],
    },
}

_BUGBEAR = {code for code in EXPECTED_CONFIG["lint"]["select"] if code.startswith("B")}
# Spelled in pieces so this module carries no directive-shaped comment text.
_NOQA = "no" + "qa"
_LINE_DIRECTIVE = re.compile(
    rf"#\s*{_NOQA}(?![A-Za-z0-9_])(?P<colon>\s*:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*))?",
    re.IGNORECASE,
)
# ruff/flake8 file-level exemptions, and every isort action comment (skip,
# skip_file, off/on, split): each one hides or re-cuts import order.
_FILE_DIRECTIVE = re.compile(r"#\s*(?:ruff|flake8|isort)\s*:", re.IGNORECASE)


def gated(code: str) -> bool:
    """True when ``select`` covers ``code`` (ruff's prefix selection)."""
    return bool(
        re.fullmatch(r"F[0-9]+", code)
        or code.startswith(("E9", "PLE"))
        or code == "E722"
        or re.fullmatch(r"I[0-9]+", code)
        or code in _BUGBEAR
    )


def _baseline() -> dict:
    return json.loads((ROOT / gate.BASELINE).read_text(encoding="utf-8"))


def _python_files() -> list[Path]:
    files = []
    for root in gate.ROOTS:
        files.extend(path for path in (ROOT / root).rglob("*.py")
                     if "__pycache__" not in path.parts)
    return sorted(files)


def _comments(text: str) -> list[str]:
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return [token.string for token in tokens if token.type == tokenize.COMMENT]


def _suppressions() -> tuple[Counter, Counter, list[str]]:
    """(per-rule directive count, per-rule file count, forbidden directives)."""
    per_rule: Counter = Counter()
    files_per_rule: Counter = Counter()
    forbidden = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        if not any(word in lowered for word in (_NOQA, "ruff", "flake8", "isort")):
            continue
        relative = path.relative_to(ROOT).as_posix()
        seen = set()
        for comment in _comments(text):
            if _FILE_DIRECTIVE.search(comment):
                forbidden.append(f"{relative}: suppression directive {comment!r}")
            for match in _LINE_DIRECTIVE.finditer(comment):
                codes = re.split(r"[\s,]+", (match.group("codes") or "").strip())
                codes = [code.upper() for code in codes if code]
                if not codes:
                    forbidden.append(f"{relative}: blanket line directive {comment!r}")
                for code in codes:
                    if gated(code):
                        per_rule[code] += 1
                        seen.add(code)
        for code in seen:
            files_per_rule[code] += 1
    return per_rule, files_per_rule, forbidden


def test_baseline_totals_per_rule_equal_the_pinned_ceiling():
    totals: dict[str, dict[str, int]] = {}
    for rules in _baseline().values():
        for rule, count in rules.items():
            entry = totals.setdefault(rule, {"count": 0, "files": 0})
            entry["count"] += count
            entry["files"] += 1
    assert totals == BASELINE_CEILING


def test_never_baselined_rules_are_absent_from_baseline_and_ceiling():
    assert {"F821", "invalid-syntax"} <= gate.NEVER_BASELINED
    in_baseline = sorted(
        f"{path} {rule}" for path, rules in _baseline().items()
        for rule in rules if rule in gate.NEVER_BASELINED
    )
    assert in_baseline == []
    assert not set(BASELINE_CEILING) & gate.NEVER_BASELINED


def test_baseline_entries_are_well_formed_and_name_existing_files():
    baseline = _baseline()
    text = (ROOT / gate.BASELINE).read_text(encoding="utf-8")
    assert text == gate.dump_baseline(baseline), "baseline must be in the gate's canonical form"
    missing = [path for path in baseline if not (ROOT / path).is_file()]
    assert missing == []
    outside = [path for path in baseline
               if not path.startswith(tuple(f"{root}/" for root in gate.ROOTS))
               or path in gate.EXCLUDED]
    assert outside == []
    assert gate.baseline_problems(baseline, set(baseline)) == []


def test_rule_set_scope_and_ruff_pin_cannot_be_loosened():
    with (ROOT / gate.CONFIG).open("rb") as handle:
        assert tomllib.load(handle) == EXPECTED_CONFIG
    assert gate.ROOTS == ("backend/app", "backend/tests", "scripts")
    assert gate.EXCLUDED == ()
    stray = sorted(
        path.relative_to(ROOT).as_posix()
        for root in gate.ROOTS
        for name in ("ruff.toml", ".ruff.toml", "pyproject.toml")
        for path in (ROOT / root).rglob(name)
    )
    assert stray == [], "a nested configuration would override ruff.toml for its subtree"
    dev_in = (ROOT / "backend" / "requirements-dev.in").read_text(encoding="utf-8")
    pin = re.search(r"^ruff==(\S+)", dev_in, re.M)
    assert pin is not None, "ruff must be pinned to one exact version"
    assert gate.pinned_ruff_version(ROOT) == pin.group(1)
    contracts = (ROOT / "scripts" / "check_contracts.sh").read_text(encoding="utf-8")
    assert '"$ROOT_DIR/scripts/check_backend_lint.py"' in contracts


def test_suppression_directives_are_pinned_and_never_blanket():
    per_rule, files_per_rule, forbidden = _suppressions()
    assert forbidden == []
    actual = {rule: {"count": per_rule[rule], "files": files_per_rule[rule]}
              for rule in sorted(per_rule)}
    assert actual == SUPPRESSION_CEILING


# ---------------------------------------------------------------- gate logic


def test_gate_reports_new_and_stale_cells():
    baseline = {"a.py": {"F401": 2}, "b.py": {"F811": 1}}
    current = {"a.py": {"F401": 3}, "c.py": {"F841": 1}}
    new, stale = gate.compare(baseline, current)
    assert new == [("a.py", "F401", 3, 2), ("c.py", "F841", 1, 0)]
    assert stale == [("b.py", "F811", 0, 1)]
    assert gate.compare(baseline, baseline) == ([], [])


def test_prune_only_lowers_and_drops_zeroes():
    baseline = {"a.py": {"F401": 3, "F811": 1}, "b.py": {"F841": 1}}
    current = {"a.py": {"F401": 1, "F811": 5}, "c.py": {"F401": 9}}
    assert gate.pruned(baseline, current) == {"a.py": {"F401": 1, "F811": 1}}


def test_baseline_problems_cover_never_baselined_bad_counts_and_unlinted_files():
    problems = gate.baseline_problems(
        {"a.py": {"F821": 1}, "b.py": {"F401": 0}, "gone.py": {"F401": 1}, "c.py": {}},
        {"a.py", "b.py", "c.py"},
    )
    assert problems == [
        "a.py F821: this rule is never baselined; fix it",
        "b.py F401: count must be a positive integer",
        "c.py: baseline entry must be a non-empty rule map",
        "gone.py: baseline names a file ruff does not lint (deleted or renamed: run --prune)",
    ]
    # --prune tolerates (and then drops) entries of files that are gone.
    assert gate.baseline_problems({"gone.py": {"F401": 1}}, set(), prune=True) == []


def test_ceiling_deltas_name_what_to_lower_after_a_prune():
    before = {"a.py": {"I001": 2, "F401": 1}, "b.py": {"I001": 1}, "gone.py": {"F811": 3}}
    after = gate.pruned(before, {"a.py": {"I001": 1, "F401": 1}})
    assert after == {"a.py": {"F401": 1, "I001": 1}}
    assert gate.ceiling_deltas(before, after) == {"F811": (3, 1), "I001": (2, 1)}


def test_code_less_diagnostics_count_as_invalid_syntax(tmp_path):
    diagnostics = [
        {"filename": str(tmp_path / "x.py"), "code": None},
        {"filename": str(tmp_path / "x.py"), "code": "F401"},
        {"filename": str(tmp_path / "x.py"), "code": "F401"},
    ]
    assert gate.current_counts(diagnostics, tmp_path.resolve()) == {
        "x.py": {"F401": 2, "invalid-syntax": 1}
    }


@pytest.mark.parametrize(
    ("comment", "rules", "forbidden"),
    [
        (f"# {_NOQA}: F401", ["F401"], False),
        (f"#{_NOQA}:F401,E402", ["F401"], False),
        (f"# {_NOQA}: E402 - sibling import", [], False),
        (f"# {_NOQA}: BLE001", [], False),
        (f"# {_NOQA}", [], True),
        (f"# {_NOQA} - reason", [], True),
        ("# ruff: " + _NOQA, [], True),
        ("# flake8: " + _NOQA, [], True),
        ("# isort: skip_file", [], True),
        ("# isort: skip", [], True),
        ("# isort: off", [], True),
        ("# isort: split", [], True),
        (f"# {_NOQA}: I001", ["I001"], False),
        (f"# {_NOQA}: B006 F811", ["B006", "F811"], False),
    ],
)
def test_directive_parsing(tmp_path, monkeypatch, comment, rules, forbidden):
    root = tmp_path / "backend" / "app"
    root.mkdir(parents=True)
    (root / "m.py").write_text(f"import os  {comment}\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    per_rule, _, found = _suppressions()
    assert sorted(per_rule.elements()) == sorted(rules)
    assert bool(found) is forbidden
