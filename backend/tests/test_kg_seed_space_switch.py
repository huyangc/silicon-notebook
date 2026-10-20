"""The concept seed-space switch (S1) and the squash candidate tier (P3) inside
real rebuilds, on SQLite. The scenarios live in ``tests/kg_seed_space_world.py``
and run on PostgreSQL in ``tests/postgres/test_kg_seed_space_switch_pg.py``."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import kg_seed_space_world as world
from tests.model_testkit import bind_all_embedding_clients

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


@pytest.mark.parametrize("name", sorted(world.SCENARIOS))
def test_seed_space_scenario(repo, monkeypatch, name):
    world.SCENARIOS[name](repo, monkeypatch)


def test_a_row_decided_again_mid_rebuild_stays_v1(repo):
    world.assert_a_row_decided_again_mid_rebuild_stays_v1(repo)


# ------------------------------------------------------------------- guard

_SCANNED = ("backend/app", "scripts")
_INSERT = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+concept_merge_candidates\b",
                     re.IGNORECASE)


def _literal_text(node: ast.AST) -> "str | None":
    """A string literal's text — an f-string's literal parts with ``{}`` for
    each placeholder (adjacent literals are already joined by the parser)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}"
                       for part in node.values)
    return None


def _insert_statements(path: Path) -> list[str]:
    """Every string literal (f-strings included) that inserts into
    concept_merge_candidates by name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        text = _literal_text(node)
        if text is not None and _INSERT.search(text):
            found.append(text)
    return found


def test_every_insert_into_merge_candidates_names_its_seed_version():
    """S1: a decision row's keys are only meaningful with the seed space they
    were minted in, and the column's default (1) is the PRE-upgrade space — an
    INSERT that leaves ``seed_version`` to the default would mint v2 keys
    labelled v1. Every writer names it explicitly. Deleting it from any one
    statement turns this red. Scanned: every Python file of the application
    and of ``scripts/`` (the copy paths insert whole snapshot rows, which
    carry the column, and name no table here)."""
    seen = 0
    for top in _SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT)
            for statement in _insert_statements(path):
                tail = _INSERT.split(statement, 1)[1]
                columns = tail.split("(", 1)[1].split(")", 1)[0]
                assert "seed_version" in {c.strip() for c in columns.split(",")}, \
                    f"{relative} inserts a merge candidate without seed_version: " \
                    f"{statement[:120]!r}"
                seen += 1
    # insert_merge_candidate, insert_pending_merge_rows and the recompute's
    # extra rows, on each backend.
    assert seen == 6
