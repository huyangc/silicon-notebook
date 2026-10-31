"""Guard for the backend lint gate: what ``scripts/check_backend_lint.py`` cannot see.

The gate (contracts lane) runs ruff over the files git lists, compares
per-(file, rule) counts with ``scripts/backend_lint_baseline.json`` and counts
the comment directives that suppress gated rules. This module covers the holes
that leaves, without starting a process:

1. Per-rule totals and file counts are pinned in ``BASELINE_CEILING`` with zero
   slack, so rewriting the baseline to absorb a new violation turns this red.
   The pin is per rule: one violation fixed in file A and one added in file B
   under the same rule keeps every number, and only review of the baseline
   diff catches that.
2. The rule set cannot be loosened: ``ruff.toml`` must equal ``EXPECTED_CONFIG``
   exactly (no ``ignore``, ``per-file-ignores``, ``exclude``, preview or
   extend keys), and the gate's roots and exclusions are pinned.
3. Suppressions cannot absorb debt: the gate's ``SUPPRESSIONS`` must equal
   ``SUPPRESSION_CEILING`` here, so raising one without the other is red.
4. Rules in ``NEVER_BASELINED`` (``F821`` undefined name and friends) are
   never recorded: such a violation is a bug and is fixed on the spot.
5. The contracts lane really runs the gate: one exact, effective command line,
   not commented out, not neutralised, not after an ``exit``.

The rest drives the gate's functions on fixtures; the one real-tree run is the
contracts lane's.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
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
    "B017": {"count": 12, "files": 7},
    "B018": {"count": 2, "files": 2},
    "B039": {"count": 1, "files": 1},
    "F401": {"count": 236, "files": 103},
    "F402": {"count": 2, "files": 1},
    "F403": {"count": 1, "files": 1},
    "F811": {"count": 402, "files": 38},
    "F841": {"count": 9, "files": 9},
    # Import order (I001, one per unsorted import block). Recorded, never
    # auto-fixed: no import was reordered when the rule was turned on.
    "I001": {"count": 1365, "files": 991},
}

#: Line suppressions that name a gated rule, per rule. These predate the gate
#: (they were written for flake8-era tooling): pytest fixtures imported for
#: injection, re-exports and side-effect imports. Only ever lowered, and
#: always equal to the gate's ``SUPPRESSIONS``.
SUPPRESSION_CEILING = {
    "F401": {"count": 65, "files": 55},
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

#: The one command line in check_contracts.sh that runs the gate.
GATE_COMMAND = '"$PYTHON_BIN" "$ROOT_DIR/scripts/check_backend_lint.py"'

# Spelled in pieces so this module carries no directive-shaped comment text.
_NOQA = "no" + "qa"
_SELECT = EXPECTED_CONFIG["lint"]["select"]


def _baseline() -> dict:
    return json.loads((ROOT / gate.BASELINE).read_text(encoding="utf-8"))


def shell_commands(source: str) -> list[str]:
    """The effective command lines of a shell script: continuation lines joined,
    comment lines and trailing comments dropped, blank lines skipped."""
    commands: list[str] = []
    pending = ""
    for raw in source.splitlines():
        line = raw.rstrip()
        if not pending and line.lstrip().startswith("#"):
            continue
        line = re.sub(r"\s+#.*$", "", line) if "#" in line else line
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        command = (pending + line.strip()).strip()
        pending = ""
        if command:
            commands.append(command)
    return commands


def gate_wiring_problems(source: str) -> list[str]:
    commands = shell_commands(source)
    problems = []
    if not commands or commands[0] != "set -euo pipefail":
        problems.append("the lane must start with `set -euo pipefail`")
    mentions = [index for index, command in enumerate(commands) if "check_backend_lint.py" in command]
    exact = [index for index in mentions if commands[index] == GATE_COMMAND]
    if len(mentions) != 1 or len(exact) != 1:
        problems.append(f"expected exactly one `{GATE_COMMAND}` command line, found {mentions and [commands[i] for i in mentions]}")
    exits = [index for index, command in enumerate(commands) if re.match(r"exit\b", command)]
    if exact and exits and exits[0] < exact[0]:
        problems.append("the gate runs after an `exit`")
    return problems


def test_baseline_totals_per_rule_equal_the_pinned_ceiling():
    totals: dict[str, dict[str, int]] = {}
    for rules in _baseline().values():
        for rule, count in rules.items():
            entry = totals.setdefault(rule, {"count": 0, "files": 0})
            entry["count"] += count
            entry["files"] += 1
    assert totals == BASELINE_CEILING


def test_suppression_totals_equal_the_pinned_ceiling():
    assert gate.SUPPRESSIONS == SUPPRESSION_CEILING


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
    assert gate.selected_rules(ROOT) == _SELECT
    assert gate.ROOTS == ("backend/app", "backend/tests", "scripts")
    assert gate.EXCLUDED == ()
    assert gate.NESTED_CONFIGS == {"ruff.toml", ".ruff.toml", "pyproject.toml"}
    dev_in = (ROOT / "backend" / "requirements-dev.in").read_text(encoding="utf-8")
    pin = re.search(r"^ruff==(\S+)", dev_in, re.M)
    assert pin is not None, "ruff must be pinned to one exact version"
    assert gate.pinned_ruff_version(ROOT) == pin.group(1)


def test_contracts_lane_runs_the_gate_once_and_lets_it_fail():
    source = (ROOT / "scripts" / "check_contracts.sh").read_text(encoding="utf-8")
    assert gate_wiring_problems(source) == []


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(lambda s: s.replace(GATE_COMMAND, "# " + GATE_COMMAND), id="commented-out"),
        pytest.param(lambda s: s.replace(GATE_COMMAND, GATE_COMMAND + " || true"), id="or-true"),
        pytest.param(lambda s: s.replace(GATE_COMMAND, GATE_COMMAND + "; true"), id="semicolon-true"),
        pytest.param(lambda s: s.replace(GATE_COMMAND + "\n", "") + "exit 0\n" + GATE_COMMAND + "\n",
                     id="after-exit"),
        pytest.param(lambda s: s.replace(GATE_COMMAND, GATE_COMMAND + "\n" + GATE_COMMAND), id="twice"),
        pytest.param(lambda s: s.replace("set -euo pipefail", "set -uo pipefail"), id="no-errexit"),
        pytest.param(lambda s: s.replace(GATE_COMMAND, GATE_COMMAND.replace(" ", " \\\n  ", 1) + " \\\n  || true"),
                     id="continued-or-true"),
    ],
)
def test_neutralised_wiring_is_rejected(rewrite):
    source = (ROOT / "scripts" / "check_contracts.sh").read_text(encoding="utf-8")
    assert gate_wiring_problems(rewrite(source)) != []


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
        "gone.py: baseline names a file ruff does not lint. Deleted: run --prune."
        " Renamed: run --rename OLD NEW",
    ]
    # --prune tolerates (and then drops) entries of files that are gone.
    assert gate.baseline_problems({"gone.py": {"F401": 1}}, set(), prune=True) == []


def test_ceiling_deltas_name_what_to_lower_after_a_prune():
    before = {"a.py": {"I001": 2, "F401": 1}, "b.py": {"I001": 1}, "gone.py": {"F811": 3}}
    after = gate.pruned(before, {"a.py": {"I001": 1, "F401": 1}})
    assert after == {"a.py": {"F401": 1, "I001": 1}}
    assert gate.ceiling_deltas(before, after) == {"F811": (3, 1), "I001": (2, 1)}


def test_rename_moves_entries_capped_at_what_the_new_path_has():
    baseline = {"old.py": {"F401": 2, "I001": 1}, "x.py": {"F811": 1}}
    current = {"new.py": {"F401": 1, "I001": 1}, "x.py": {"F811": 1}}
    assert gate.renamed(baseline, current, {"new.py", "x.py"}, "old.py", "new.py") == {
        "new.py": {"F401": 1, "I001": 1}, "x.py": {"F811": 1},
    }
    for old, new, linted, message in [
        ("nope.py", "new.py", {"new.py"}, "has no baseline entries"),
        ("old.py", "new.py", {"old.py", "new.py"}, "is still linted"),
        ("old.py", "gone.py", {"new.py"}, "is not a file the gate lints"),
        ("old.py", "x.py", {"x.py"}, "already has baseline entries"),
    ]:
        with pytest.raises(gate.GateError, match=message):
            gate.renamed(baseline, current, linted, old, new)


def test_code_less_diagnostics_count_as_invalid_syntax(tmp_path):
    root = tmp_path.resolve()
    diagnostics = [
        {"filename": str(root / "x.py"), "code": None},
        {"filename": str(root / "x.py"), "code": "F401"},
        {"filename": str(root / "x.py"), "code": "F401"},
    ]
    assert gate.current_counts(diagnostics, root) == {"x.py": {"F401": 2, "invalid-syntax": 1}}


def test_file_listing_is_nul_separated_and_keeps_non_ascii_names():
    listed = "backend/app/a.py\0scripts/诊断 工具.py\0backend/tests/b c.py\0".encode()
    assert gate.parse_ls_files(listed) == ["backend/app/a.py", "scripts/诊断 工具.py", "backend/tests/b c.py"]


def test_paths_outside_the_repository_are_a_gate_error_not_a_traceback(tmp_path):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    assert gate.relative_path(str(root / "scripts" / "诊断.py"), root) == "scripts/诊断.py"
    assert gate.relative_path("scripts/x.py", root) == "scripts/x.py"
    with pytest.raises(gate.GateError, match="outside the repository"):
        gate.relative_path(str(tmp_path / "elsewhere.py"), root)


def test_symbolic_links_are_not_followed_when_mapping_paths(tmp_path):
    root = (tmp_path / "repo").resolve()
    (root / "scripts").mkdir(parents=True)
    (tmp_path / "target.py").write_text("x = 1\n", encoding="utf-8")
    (root / "scripts" / "link.py").symlink_to(tmp_path / "target.py")
    assert gate.relative_path(str(root / "scripts" / "link.py"), root) == "scripts/link.py"


def test_a_missing_ruff_install_is_a_gate_error(monkeypatch):
    def missing(name):
        raise gate.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(gate.metadata, "version", missing)
    with pytest.raises(gate.GateError, match="requirements-dev.txt"):
        gate.installed_ruff_version()


@pytest.mark.parametrize(
    ("code", "covered"),
    [("F401", True), ("FBT001", False), ("E902", True), ("E501", False), ("E722", True),
     ("PLE0101", True), ("PLW0603", False), ("I001", True), ("ISC001", False),
     ("B006", True), ("B008", False), ("UP006", False)],
)
def test_gated_follows_ruff_selection(code, covered):
    assert gate.gated(code, _SELECT) is covered


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
def test_directive_parsing(comment, rules, forbidden):
    codes, found = gate.directive_findings(f"import os  {comment}\n", _SELECT)
    assert sorted(codes) == sorted(rules)
    assert bool(found) is forbidden


def test_directive_text_inside_strings_is_not_a_directive():
    text = f'MESSAGE = "# {_NOQA}"\nOTHER = """# isort: skip_file"""\n'
    assert gate.directive_findings(text, _SELECT) == ([], [])
