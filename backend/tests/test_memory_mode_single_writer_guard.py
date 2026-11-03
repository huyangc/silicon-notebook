"""Closed inventory of SQL writers for the user-owned memory suggestion switch."""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

from tests.architecture.source_trees import app_source_trees, read_source_tree

pytestmark = pytest.mark.xdist_group("source_scans_core")

ROOT = Path(__file__).resolve().parents[2]
_IDENTIFIER = r'(?:[a-zA-Z_]\w*|"[^"]+"|`[^`]+`|\[[^\]]+\])'
_PROFILE_TABLE = r'(?:user_profiles|"user_profiles"|`user_profiles`|\[user_profiles\])'
WRITES = re.compile(
    r"\b(?:UPDATE(?:\s+ONLY)?|INSERT(?:\s+OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK))?\s+INTO)\s+"
    rf"(?:{_IDENTIFIER}\s*\.\s*)?{_PROFILE_TABLE}(?=\s|\(|$)", re.I,
)
SWITCH = {
    (f"repositories/{backend}/memory_suggest_store.py", "MemorySuggestStore.set_memory_mode"): 2
    for backend in ("postgres", "sqlite")
}
DEFAULT_INSERTS = {
    ("repositories/auth_store.py", "AuthStore.create"): 1,
    ("repositories/postgres/bundle.py", "_initialize"): 1,
    ("repositories/sqlite/migrations.py", "SqliteMigrator._seed"): 1,
    **{(f"repositories/{backend}/identity_store.py", f"IdentityStore.{method}"): 1
       for backend in ("postgres", "sqlite")
       for method in ("_create_user_in_txn", "set_user_ui_mode", "set_user_search_profile")},
}
MERGE = {("scripts/merge_dbs.py", "fold_memory_mode_to_switch"): 1}


def _writes(tree, scope=""):
    if isinstance(tree, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        scope = f"{scope}.{tree.name}" if scope else tree.name
    if isinstance(tree, ast.Constant) and isinstance(tree.value, str):
        sql = re.sub(r"/\*.*?\*/|--[^\n]*", "", tree.value, flags=re.S)
        if WRITES.search(sql) and re.search(r"\bmemory_mode\b", sql):
            yield scope, sql
    for child in ast.iter_child_nodes(tree):
        yield from _writes(child, scope)


def _check(sources):
    found = Counter()
    for relative, tree in sources:
        for scope, sql in _writes(tree):
            key = relative, scope
            found[key] += 1
            if key in DEFAULT_INSERTS and relative != "repositories/sqlite/migrations.py":
                assert re.search(r"'manual'", sql), key
            if key in MERGE:
                assert re.search(r"SET\s+memory_mode\s*=\s*'manual'", sql, re.I), key
        if relative == "repositories/sqlite/migrations.py":
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) < 2:
                    continue
                sql, params = node.args[:2]
                if (isinstance(sql, ast.Constant) and isinstance(sql.value, str)
                        and WRITES.search(sql.value) and "memory_mode" in sql.value):
                    assert isinstance(params, ast.Tuple)
                    assert isinstance(params.elts[2], ast.Constant)
                    assert params.elts[2].value == "manual"
    assert found == Counter(SWITCH | DEFAULT_INSERTS | MERGE), found


@pytest.fixture(scope="module")
def registered_sources():
    sources = [(source.relative, source.tree) for source in app_source_trees()]
    # The frozen-v9 fixture constructor is deliberately not production code.
    sources.extend((p.relative_to(ROOT).as_posix(), read_source_tree(p))
                   for p in (ROOT / "scripts").rglob("*.py")
                   if p.name != "generate_repository_contract_fixtures.py")
    # Discover against every production module once; mutation cases only
    # need to replay the small set with writes plus their injected module.
    return [(relative, tree) for relative, tree in sources if any(_writes(tree))]


def test_memory_mode_has_only_the_registered_writers(registered_sources):
    _check(registered_sources)


@pytest.mark.parametrize("relative", [
    "services/memory_suggest_job.py", "repositories/sqlite/identity_store.py",
])
@pytest.mark.parametrize("statement", [
    "UPDATE user_profiles SET memory_mode = 'suggest'",
    'UPDATE "user_profiles" SET memory_mode = \'suggest\'',
    'UPDATE "public"."user_profiles" SET memory_mode = \'suggest\'',
    "UPDATE main.`user_profiles` SET memory_mode = 'suggest'",
    "UPDATE [user_profiles] SET memory_mode = 'suggest'",
    'INSERT INTO "user_profiles" (memory_mode) VALUES (\'suggest\')',
    'INSERT INTO "public"."user_profiles" (memory_mode) VALUES (\'suggest\')',
])
def test_a_new_writer_or_one_moved_to_another_site_is_rejected(relative, statement, registered_sources):
    mutation = ast.parse(f"def unauthorized_writer(db):\n    db.execute({statement!r})")
    assert list(_writes(mutation))
    with pytest.raises(AssertionError):
        _check([*registered_sources, (relative, mutation)])
