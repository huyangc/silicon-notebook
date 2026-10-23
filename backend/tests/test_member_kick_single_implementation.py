"""Ending every link membership of a notebook has one implementation per backend.

User ruling 2026-10-23: unsharing the link (``clear_share``) is the notebook-wide
member removal, and it removes the rows through the store primitive
``kick_all_members`` on its own connection, which returns who was removed so the
service can clear each person's private state the way ``remove_member`` does.
A second, inline ``DELETE FROM notebook_members WHERE notebook_id=…`` would be a
second implementation that returns no one, and the per-member cleanup would
silently stop for it. The behaviour is pinned by ``tests/memory_purge_cases.py``
(``unshare_*``); this guard pins the shape, which behaviour cannot tell apart.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

STORES = (
    Path(__file__).resolve().parents[1] / "app/repositories/sqlite/sharing_store.py",
    Path(__file__).resolve().parents[1] / "app/repositories/postgres/sharing_store.py",
)


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _notebook_wide_member_deletes(node: ast.AST) -> list[str]:
    """String constants deleting ``notebook_members`` rows by notebook only."""
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            text = " ".join(child.value.split())
            if "DELETE FROM notebook_members" not in text:
                continue
            where = text.split("WHERE", 1)[-1].split("RETURNING", 1)[0]
            if "user_id" not in where:
                found.append(text)
    return found


@pytest.mark.parametrize("path", STORES, ids=lambda p: p.parent.name)
def test_the_notebook_wide_member_delete_lives_only_in_kick_all_members(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = _functions(tree)
    everywhere = _notebook_wide_member_deletes(tree)
    in_primitive = _notebook_wide_member_deletes(functions["kick_all_members"])
    assert len(in_primitive) == 1, in_primitive
    assert everywhere == in_primitive, everywhere


@pytest.mark.parametrize("path", STORES, ids=lambda p: p.parent.name)
def test_clear_share_removes_members_through_the_primitive_and_returns_them(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    clear_share = _functions(tree)["clear_share"]
    returned_calls = [
        node.value
        for node in ast.walk(clear_share)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
    ]
    assert any(
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "kick_all_members"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        for call in returned_calls
    ), "clear_share must return self.kick_all_members(<its connection>, notebook_id)"
