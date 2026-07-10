from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from pathlib import Path
import typing

from app.services import sqlite_repository
from app.services.sqlite_repository import SQLiteRepository


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "facade_surface.json"
)
GENERATOR = ROOT / "scripts" / "generate_repository_contract_fixtures.py"

REQUIRED_GENERATOR_CALLABLES = {
    "collect_facade_surface",
    "generate_v9_fixture",
    "normalized_repository_snapshot",
    "generate_ask_goldens",
    "generate_api_contract",
    "main",
}
REQUIRED_MEMBER_FIELDS = {
    "kind",
    "signature",
    "consumers",
    "owner",
    "patch_targets",
}


def _surface() -> dict[str, dict[str, object]]:
    assert FIXTURE.is_file(), f"missing frozen facade surface: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_facade_surface_manifest_is_complete_and_owned():
    surface = _surface()

    assert surface
    assert {"create_notebook", "ask", "ask_chunk", "llm_client"} <= set(surface)
    for name, record in surface.items():
        assert REQUIRED_MEMBER_FIELDS <= set(record), name
        assert record["kind"] in {
            "method",
            "private_wrapper",
            "property",
            "mutable_property",
            "instance_attribute",
            "constant",
        }
        assert isinstance(record["signature"], str)
        assert record["consumers"], name
        assert isinstance(record["owner"], str) and record["owner"], name
        assert isinstance(record["patch_targets"], list), name


def test_every_patch_target_has_a_migration_record():
    patch_targets = [
        patch
        for record in _surface().values()
        for patch in record["patch_targets"]
    ]

    assert patch_targets
    for patch in patch_targets:
        assert set(patch) == {
            "file",
            "line",
            "target",
            "compatibility",
        }
        assert patch["file"].startswith("backend/tests/")
        assert isinstance(patch["line"], int) and patch["line"] > 0
        assert patch["target"]
        assert patch["compatibility"] in {
            "production-compatible",
            "test-only",
        }


def test_frozen_members_still_exist_with_the_same_callable_signatures():
    for name, record in _surface().items():
        kind = record["kind"]
        if record.get("scope") == "module":
            member = getattr(sqlite_repository, name)
            if kind == "constant":
                continue
            assert callable(member), name
            assert str(inspect.signature(member)) == record["signature"], name
            continue
        if kind == "constant":
            assert hasattr(sqlite_repository, name), name
            continue
        if kind in {"instance_attribute", "mutable_property"} and not hasattr(
            SQLiteRepository, name
        ):
            continue

        member = inspect.getattr_static(SQLiteRepository, name)
        if kind == "property":
            assert isinstance(member, property) and member.fset is None, name
            signature = str(inspect.signature(member.fget))
        elif kind == "mutable_property":
            assert isinstance(member, property) and member.fset is not None, name
            signature = str(inspect.signature(member.fget))
        else:
            assert callable(member), name
            signature = str(inspect.signature(member))
        assert signature == record["signature"], name


def test_generator_exposes_the_frozen_public_callable_set():
    assert GENERATOR.is_file(), f"missing fixture generator: {GENERATOR}"
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"), filename=str(GENERATOR))
    callables = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert REQUIRED_GENERATOR_CALLABLES <= callables


def test_snapshot_generator_annotation_resolves_to_the_facade_type():
    spec = importlib.util.spec_from_file_location("repository_fixture_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    hints = typing.get_type_hints(module.normalized_repository_snapshot)
    assert hints == {
        "repo": SQLiteRepository,
        "notebook_id": str,
        "return": dict[str, object],
    }
