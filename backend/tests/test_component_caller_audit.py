"""Unit pins for ``scripts/audit_component_callers.py``.

The full census over the real tree runs as ``--check`` in the contracts lane
(``scripts/check_contracts.sh``); these pin the pieces that decide what counts
as a reference and when the gate goes red, without the ~8s whole-tree scan.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "audit_component_callers", ROOT / "scripts" / "audit_component_callers.py"
)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules.setdefault(_SPEC.name, audit)  # dataclasses resolve their module
_SPEC.loader.exec_module(audit)


def _name_arg(source: str) -> ast.AST:
    call = ast.parse(source, mode="eval").body
    assert isinstance(call, ast.Call)
    return call.args[1]


def test_run_time_names_built_from_literal_pieces_reference_every_fitting_member():
    chain = audit._template_regex(_name_arg('getattr(jobs, f"delete_{chain}_page")'))
    assert chain is not None
    assert chain.match("delete_source_elements_page")
    assert not chain.match("delete_source_elements")
    migration = audit._template_regex(_name_arg('getattr(self, "_migration_" + str(v))'))
    assert migration is not None and migration.match("_migration_56")
    # A name with no literal piece at all cannot be attributed to anything.
    assert audit._template_regex(_name_arg("getattr(obj, name)")) is None
    assert audit._template_regex(_name_arg('getattr(obj, f"{name}")')) is None


def _refs(source: str, rel: str = "backend/app/x.py", index=None):
    collector = audit._RefCollector(rel, index or {})
    collector.visit(ast.parse(source))
    return collector.refs


def test_references_carry_their_enclosing_member_and_skip_docstrings():
    source = '''
class A:
    """mentions helper in prose only"""

    def m(self):
        """calls helper, says the docstring"""
        self.helper()
        return getattr(self, "probe")

    @property
    def p(self):
        return 1

    @p.setter
    def p(self, value):
        self._p = value
'''
    index = {("A", "m"): "backend/app/x.py::A.m", ("A", "p"): "backend/app/x.py::A.p"}
    refs = _refs(source, index=index)
    helper = [r for r in refs if r.name == "helper"]
    assert [r.owner for r in helper] == ["backend/app/x.py::A.m"]
    assert [r.owner for r in refs if r.name == "probe"] == ["backend/app/x.py::A.m"]
    # ``@p.setter`` defines the property, it does not read it.
    assert not [r for r in refs if r.name == "p"]


def test_dotted_entry_point_strings_reference_their_last_segment():
    refs = _refs('ENTRY = "pkg.module:Factory.build"\n')
    assert {"pkg", "module", "Factory", "build"} <= {r.name for r in refs}


def _report(*candidates):
    return {
        "summary": {},
        "candidates": [
            {"member": key, "bucket": bucket, "extension_surface": surface, "reason": ""}
            for key, bucket, surface in candidates
        ],
        "exempt": [],
    }


OBSERVED = "backend/app/services/pending_bus.py::PendingBus._buffered_count"
UNMARKED = "backend/app/repositories/sqlite/unified_kg_store.py::UnifiedKgStore.community_overview"


def test_check_passes_when_every_candidate_is_registered_once():
    report = _report((OBSERVED, "test-only", False))
    allowlist = {OBSERVED: {"member": OBSERVED, "category": "test-observation", "reason": "r"}}
    assert audit.check(report, allowlist) == []


@pytest.mark.parametrize(
    ("candidates", "allowlist", "fragment"),
    [
        pytest.param(
            [("backend/app/a.py::A.new", "test-only", False)], {},
            "test-only component member", id="unregistered-new-member",
        ),
        pytest.param(
            [], {"backend/app/a.py::A.gone": {"category": "test-observation", "reason": "r"}},
            "no longer a candidate", id="stale-entry",
        ),
        pytest.param(
            [("backend/app/a.py::A.port", "test-only", False)],
            {"backend/app/a.py::A.port": {"category": "extension-port", "reason": "r"}},
            "not extension surface", id="extension-port-off-surface",
        ),
        pytest.param(
            [("backend/app/a.py::A.x", "test-only", False)],
            {"backend/app/a.py::A.x": {"category": "keep-forever", "reason": "r"}},
            "unknown category", id="unknown-category",
        ),
        pytest.param(
            [("backend/app/a.py::A.x", "test-only", False)],
            {"backend/app/a.py::A.x": {"category": "pending-decision", "reason": " "}},
            "without a reason", id="missing-reason",
        ),
        pytest.param(
            [(UNMARKED, "test-only", False)],
            {UNMARKED: {"category": "test-observation", "reason": "r"}},
            "carries no", id="observation-without-marker",
        ),
    ],
)
def test_check_fails_on_every_drift(candidates, allowlist, fragment):
    problems = audit.check(_report(*candidates), allowlist)
    assert problems and any(fragment in problem for problem in problems), problems


def test_observation_marker_is_read_from_the_comment_above_the_def():
    assert audit.carries_observation_marker(OBSERVED)
    assert not audit.carries_observation_marker(UNMARKED)


def test_allowlist_is_sorted_unique_and_fully_described():
    data = json.loads(audit.ALLOWLIST_PATH.read_text(encoding="utf-8"))
    keys = [entry["member"] for entry in data["members"]]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    for entry in data["members"]:
        assert entry["category"] in audit.CATEGORIES, entry
        assert entry["reason"].strip(), entry
        rel = entry["member"].split("::", 1)[0]
        assert (ROOT / rel).is_file(), entry


# ---------------------------------------------------------------- classification


def _member(cls: str, name: str) -> "audit.Member":
    return audit.Member("backend/app/x.py", "app.x", cls, name, 1)


def _ref(name: str, owner: "audit.Member | None", rel: str = "backend/app/x.py") -> "audit.Ref":
    return audit.Ref(name, None, rel, 1, owner.key if owner else None)


def _classify(members, prod, tests, kept=()):
    return audit.classify_members(
        members, prod, tests,
        exempt_reason=lambda _m: None, importable=lambda _m: True, kept=set(kept),
    )


def test_a_member_called_only_from_a_test_only_member_is_a_candidate_too():
    a, b, c = _member("A", "a"), _member("B", "b"), _member("C", "c")
    # production: b's body calls a, module code uses c; only tests call b.
    prod = [_ref("a", b), _ref("c", None)]
    tests = [_ref("b", None, "backend/tests/test_x.py")]
    verdicts = _classify([a, b, c], prod, tests)
    assert verdicts[b.key].bucket == "test-only"
    assert verdicts[a.key].bucket == "unreferenced"  # its one caller is dead
    assert verdicts[c.key].bucket == "keep"


def test_the_fixed_point_follows_a_chain_of_any_length():
    """c is called only by b, b only by a, a only by tests: one pass would
    still see b's body as live and keep c; only the fixed point files it."""
    a, b, c = _member("A", "a"), _member("B", "b"), _member("C", "c")
    prod = [_ref("b", a), _ref("c", b)]
    tests = [_ref("a", None, "backend/tests/test_x.py")]
    verdicts = _classify([a, b, c], prod, tests)
    assert verdicts[a.key].bucket == "test-only"
    assert verdicts[b.key].bucket == "unreferenced"
    assert verdicts[c.key].bucket == "unreferenced"


def test_a_registered_member_keeps_what_it_calls_alive():
    a, b = _member("A", "a"), _member("B", "b")
    prod = [_ref("a", b)]
    tests = [_ref("b", None, "backend/tests/test_x.py")]
    verdicts = _classify([a, b], prod, tests, kept={b.key})
    assert verdicts[b.key].bucket == "test-only"  # still listed, registered
    assert verdicts[a.key].bucket == "keep"


def test_recursion_is_not_a_production_caller():
    a = _member("A", "a")
    verdicts = _classify([a], [_ref("a", a)], [_ref("a", None, "backend/tests/t.py")])
    assert verdicts[a.key].bucket == "test-only"


@pytest.mark.parametrize(
    ("decorator", "registers"),
    [
        ("field_validator", True), ("pydantic.model_validator", True),
        ("computed_field", True), ("router.get", True), ("app.command", True),
        ("atexit.register", True),
        ("get", False), ("contextmanager", False), ("contextlib.asynccontextmanager", False),
        ("functools.lru_cache", False), ("cache", False), ("functools.wraps", False),
    ],
)
def test_only_known_registering_decorators_exempt_a_member(decorator, registers):
    assert audit.is_registering_decorator(decorator) is registers


def test_extension_surface_needs_the_whole_protocol_not_one_shared_name():
    from typing import Protocol

    class Port(Protocol):
        def search(self) -> None: ...
        def fetch(self) -> None: ...

    class Full:
        def search(self):
            return None

        def fetch(self):
            return None

    class SharesOneName:
        def search(self):
            return None

    protocol_members = [(Port, audit._own_members(Port))]
    member = audit.Member("backend/app/services/x.py", "app.services.x", "C", "search", 1)
    assert audit._is_extension_surface(member, Full, protocol_members, set())
    assert not audit._is_extension_surface(member, SharesOneName, protocol_members, set())
